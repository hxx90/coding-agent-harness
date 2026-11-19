from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import JsonValue

from vibe.core.hardware.models import (
    CommandReceipt,
    CommandStatus,
    HardwareCommand,
    HardwarePolicyResult,
    HardwarePolicyStep,
    PolicyStepKind,
    PolicyTaskOutcome,
    StopReceipt,
    VerificationReport,
    VerificationStatus,
)
from vibe.core.hardware.runtime import HardwareRuntime, HardwareRuntimeError

_MAX_SOURCE_BYTES = 32 * 1024
_MAX_VALUE_BYTES = 16 * 1024
_MAX_STDOUT_LINE = 4096
_MAX_STDOUT_LINES = 100
_MAX_ACTIONS = 50
_MAX_TRACE_STEPS = 200
_MAX_AST_NODES = 1000
_MAX_ROUND_ARGUMENTS = 2
_MAX_RANGE_ARGUMENTS = 3
_PURE_CALLS = frozenset({
    "abs",
    "bool",
    "float",
    "int",
    "len",
    "max",
    "min",
    "range",
    "round",
    "str",
})
_POLICY_CALLS = frozenset({"act", "observe", "print", "stop", "verify"})
_ALLOWED_NODES = frozenset({
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Call,
    ast.keyword,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
})


class HardwarePolicyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        line: int | None = None,
        column: int | None = None,
        source_uri: str | None = None,
    ) -> None:
        self.code = code
        self.line = line
        self.column = column
        self.source_uri = source_uri
        super().__init__(message)

    def with_source(self, source_uri: str) -> HardwarePolicyError:
        return HardwarePolicyError(
            str(self),
            code=self.code,
            line=self.line,
            column=self.column,
            source_uri=source_uri,
        )


class HardwarePolicyRunner:
    """Interpret a bounded Python policy against one leased hardware device."""

    def __init__(
        self,
        runtime: HardwareRuntime,
        *,
        artifact_dir: Path,
        max_statements: int = 200,
        max_actions: int = 50,
        max_loop_iterations: int = 50,
        timeout_seconds: float = 120.0,
    ) -> None:
        if min(max_statements, max_actions, max_loop_iterations) < 1:
            raise ValueError("Hardware policy limits must be positive")
        if max_actions > _MAX_ACTIONS:
            raise ValueError(
                f"Hardware policies support at most {_MAX_ACTIONS} actions"
            )
        if timeout_seconds <= 0:
            raise ValueError("Hardware policy timeout must be positive")
        self._runtime = runtime
        self._artifact_dir = artifact_dir
        self._max_statements = max_statements
        self._max_actions = max_actions
        self._max_loop_iterations = max_loop_iterations
        self._timeout_seconds = timeout_seconds

    async def run(  # noqa: PLR0914, PLR0915
        self, source: str, *, device_id: str, lease_id: str, run_id: str
    ) -> HardwarePolicyResult:
        encoded = source.encode("utf-8")
        if not source.strip():
            raise HardwarePolicyError("Policy source is empty", code="empty_policy")
        if len(encoded) > _MAX_SOURCE_BYTES:
            raise HardwarePolicyError(
                f"Policy source exceeds {_MAX_SOURCE_BYTES} bytes",
                code="policy_too_large",
            )
        policy_id = uuid4().hex
        try:
            source_uri = await asyncio.to_thread(
                self._persist_source, policy_id=policy_id, source=source
            )
        except OSError as exc:
            raise HardwarePolicyError(
                "Could not persist hardware policy source", code="artifact_write_failed"
            ) from exc
        manifests = await self._runtime.discover()
        manifest = next(
            (item for item in manifests if item.device_id == device_id), None
        )
        if manifest is None:
            raise HardwarePolicyError(
                f"Unknown Robo device: {device_id}",
                code="device_not_found",
                source_uri=source_uri,
            )
        action_names = {action.name for action in manifest.actions}
        verification_names = {
            verification.name for verification in manifest.verifications
        }
        geometry_library = manifest.metadata.get("geometry_library", "off")
        capability_calls = (
            {"resting_geometry"} if geometry_library == "h1-v8-v9" else set()
        )
        try:
            module = _parse_policy(
                source,
                action_names=action_names,
                verification_names=verification_names,
                capability_calls=capability_calls,
            )
        except HardwarePolicyError as exc:
            raise exc.with_source(source_uri) from exc
        interpreter = _PolicyInterpreter(
            runtime=self._runtime,
            policy_id=policy_id,
            device_id=device_id,
            lease_id=lease_id,
            run_id=run_id,
            action_names=action_names,
            verification_names=verification_names,
            capability_calls=capability_calls,
            max_statements=self._max_statements,
            max_actions=self._max_actions,
            max_loop_iterations=self._max_loop_iterations,
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await interpreter.execute(module)
                snapshot = await self._runtime.observe(device_id, run_id=run_id)
                pending = await self._runtime.pending_verification_revisions()
        except asyncio.CancelledError:
            self._persist_failure(
                policy_id, interpreter, "cancelled", str(geometry_library)
            )
            await self._stop_after_failure(device_id, run_id, "policy cancelled")
            raise
        except TimeoutError as exc:
            self._persist_failure(
                policy_id, interpreter, "timeout", str(geometry_library)
            )
            await self._stop_after_failure(device_id, run_id, "policy timed out")
            raise HardwarePolicyError(
                "Hardware policy timed out",
                code="policy_timeout",
                source_uri=source_uri,
            ) from exc
        except HardwarePolicyError as exc:
            self._persist_failure(
                policy_id, interpreter, exc.code, str(geometry_library)
            )
            await self._stop_after_failure(device_id, run_id, "policy failed")
            raise exc.with_source(source_uri) from exc
        except HardwareRuntimeError as exc:
            await self._stop_after_failure(device_id, run_id, "policy hardware error")
            raise HardwarePolicyError(
                str(exc), code=exc.code, source_uri=source_uri
            ) from exc
        except Exception as exc:
            await self._stop_after_failure(device_id, run_id, "policy internal error")
            raise HardwarePolicyError(
                "Hardware policy execution failed",
                code="policy_internal",
                source_uri=source_uri,
            ) from exc

        report = interpreter.last_report
        if report is None:
            outcome = PolicyTaskOutcome.NOT_VERIFIED
        elif report.status is not VerificationStatus.PASSED:
            outcome = PolicyTaskOutcome(report.status.value)
        elif device_id in pending:
            outcome = PolicyTaskOutcome.NOT_VERIFIED
        else:
            outcome = PolicyTaskOutcome.PASSED
        result = HardwarePolicyResult(
            policy_id=policy_id,
            source=source,
            source_sha256=hashlib.sha256(encoded).hexdigest(),
            source_uri=source_uri,
            steps=interpreter.steps,
            stdout=interpreter.stdout,
            receipts=interpreter.receipts,
            snapshot=snapshot,
            report=report,
            task_outcome=outcome,
            geometry_library=str(geometry_library),
        )
        try:
            await asyncio.to_thread(
                (self._artifact_dir / f"{policy_id}.json").write_text,
                result.model_dump_json(indent=2),
            )
        except OSError as exc:
            await self._stop_after_failure(
                device_id, run_id, "policy evidence write failed"
            )
            raise HardwarePolicyError(
                "Could not persist policy result",
                code="artifact_write_failed",
                source_uri=source_uri,
            ) from exc
        return result

    def _persist_failure(
        self, policy_id: str, interpreter: _PolicyInterpreter, error: str, library: str
    ) -> None:
        # Evidence persistence must never prevent the failure-stop path.
        try:
            (self._artifact_dir / f"{policy_id}.json").write_text(
                json.dumps(
                    {
                        "policy_id": policy_id,
                        "error": error,
                        "geometry_library": library,
                        "steps": [
                            step.model_dump(mode="json") for step in interpreter.steps
                        ],
                    },
                    indent=2,
                )
            )
        except OSError:
            pass

    def _persist_source(self, *, policy_id: str, source: str) -> str:
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        path = (self._artifact_dir / f"{policy_id}.py").resolve()
        temporary = path.with_suffix(".py.tmp")
        temporary.write_text(source, encoding="utf-8")
        temporary.replace(path)
        return path.as_uri()

    async def _stop_after_failure(
        self, device_id: str, run_id: str, reason: str
    ) -> StopReceipt | None:
        try:
            async with asyncio.timeout(10):
                return await self._runtime.stop(device_id, reason=reason, run_id=run_id)
        except (HardwareRuntimeError, TimeoutError):
            return None


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


class _PolicyInterpreter:
    def __init__(  # noqa: PLR0913
        self,
        *,
        runtime: HardwareRuntime,
        policy_id: str,
        device_id: str,
        lease_id: str,
        run_id: str,
        action_names: set[str],
        verification_names: set[str],
        capability_calls: set[str],
        max_statements: int,
        max_actions: int,
        max_loop_iterations: int,
    ) -> None:
        self._capability_calls = capability_calls
        self._runtime = runtime
        self._policy_id = policy_id
        self._device_id = device_id
        self._lease_id = lease_id
        self._run_id = run_id
        self._action_names = action_names
        self._direct_action_names = _direct_action_names(action_names)
        self._verification_names = verification_names
        self._max_statements = max_statements
        self._max_actions = max_actions
        self._max_loop_iterations = max_loop_iterations
        self._statement_count = 0
        self._action_count = 0
        self._loop_iterations = 0
        self._variables: dict[str, Any] = {}
        self.steps: list[HardwarePolicyStep] = []
        self.stdout: list[str] = []
        self.receipts: list[CommandReceipt] = []
        self.last_report: VerificationReport | None = None

    async def execute(self, module: ast.Module) -> None:
        await self._execute_block(module.body)

    async def _execute_block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            await self._execute_statement(statement)

    async def _execute_statement(  # noqa: PLR0911, PLR0912, PLR0915
        self, statement: ast.stmt
    ) -> None:
        self._statement_count += 1
        if self._statement_count > self._max_statements:
            raise self._error(
                statement,
                f"Policy exceeded {self._max_statements} executed statements",
                code="statement_limit",
            )
        try:
            if isinstance(statement, ast.Expr):
                await self._evaluate(statement.value)
                return
            if isinstance(statement, ast.Assign):
                value = await self._evaluate(statement.value)
                target = cast(ast.Name, statement.targets[0])
                self._variables[target.id] = value
                return
            if isinstance(statement, ast.AugAssign):
                target = cast(ast.Name, statement.target)
                if target.id not in self._variables:
                    raise self._error(
                        statement,
                        f"Unknown policy variable: {target.id}",
                        code="unknown_name",
                    )
                right = await self._evaluate(statement.value)
                self._variables[target.id] = self._binary(
                    statement.op, self._variables[target.id], right, statement
                )
                return
            if isinstance(statement, ast.If):
                branch = (
                    statement.body
                    if await self._truth(statement.test)
                    else statement.orelse
                )
                await self._execute_block(branch)
                return
            if isinstance(statement, ast.For):
                iterable = await self._evaluate(statement.iter)
                if not isinstance(iterable, (range, list, tuple)):
                    raise self._error(
                        statement,
                        "Policy for-loops require range, list, or tuple",
                        code="invalid_loop",
                    )
                broke = False
                for item in iterable:
                    self._consume_loop_iteration(statement)
                    target = cast(ast.Name, statement.target)
                    self._variables[target.id] = item
                    try:
                        await self._execute_block(statement.body)
                    except _ContinueSignal:
                        continue
                    except _BreakSignal:
                        broke = True
                        break
                if not broke:
                    await self._execute_block(statement.orelse)
                return
            if isinstance(statement, ast.While):
                broke = False
                while await self._truth(statement.test):
                    self._consume_loop_iteration(statement)
                    try:
                        await self._execute_block(statement.body)
                    except _ContinueSignal:
                        continue
                    except _BreakSignal:
                        broke = True
                        break
                if not broke:
                    await self._execute_block(statement.orelse)
                return
            if isinstance(statement, ast.Break):
                raise _BreakSignal
            if isinstance(statement, ast.Continue):
                raise _ContinueSignal
            if isinstance(statement, ast.Pass):
                return
            raise self._error(statement, "Unsupported policy statement", code="syntax")
        except (HardwarePolicyError, _BreakSignal, _ContinueSignal):
            raise
        except HardwareRuntimeError as exc:
            raise self._error(statement, str(exc), code=exc.code) from exc
        except (ArithmeticError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise self._error(
                statement, str(exc) or type(exc).__name__, code="policy_runtime"
            ) from exc

    async def _evaluate(self, expression: ast.expr) -> Any:  # noqa: PLR0911, PLR0912
        if isinstance(expression, ast.Constant):
            return expression.value
        if isinstance(expression, ast.Name):
            if expression.id not in self._variables:
                raise self._error(
                    expression,
                    f"Unknown policy variable: {expression.id}",
                    code="unknown_name",
                )
            return self._variables[expression.id]
        if isinstance(expression, ast.List):
            return [await self._evaluate(item) for item in expression.elts]
        if isinstance(expression, ast.Tuple):
            return tuple([await self._evaluate(item) for item in expression.elts])
        if isinstance(expression, ast.Dict):
            keys = [
                await self._evaluate(cast(ast.expr, item)) for item in expression.keys
            ]
            values = [await self._evaluate(item) for item in expression.values]
            return dict(zip(keys, values, strict=True))
        if isinstance(expression, ast.Subscript):
            value = await self._evaluate(expression.value)
            key = await self._evaluate(expression.slice)
            return value[key]
        if isinstance(expression, ast.Call):
            return await self._call(expression)
        if isinstance(expression, ast.BinOp):
            return self._binary(
                expression.op,
                await self._evaluate(expression.left),
                await self._evaluate(expression.right),
                expression,
            )
        if isinstance(expression, ast.UnaryOp):
            value = await self._evaluate(expression.operand)
            if isinstance(expression.op, ast.Not):
                return not bool(value)
            if isinstance(expression.op, ast.USub):
                return -value
            if isinstance(expression.op, ast.UAdd):
                return +value
        if isinstance(expression, ast.BoolOp):
            if isinstance(expression.op, ast.And):
                result: Any = True
                for item in expression.values:
                    result = await self._evaluate(item)
                    if not result:
                        return result
                return result
            result = False
            for item in expression.values:
                result = await self._evaluate(item)
                if result:
                    return result
            return result
        if isinstance(expression, ast.Compare):
            left = await self._evaluate(expression.left)
            for operation, comparator in zip(
                expression.ops, expression.comparators, strict=True
            ):
                right = await self._evaluate(comparator)
                if not self._compare(operation, left, right):
                    return False
                left = right
            return True
        raise self._error(expression, "Unsupported policy expression", code="syntax")

    async def _truth(self, expression: ast.expr) -> bool:
        return bool(await self._evaluate(expression))

    async def _call(self, call: ast.Call) -> Any:  # noqa: PLR0911
        name = cast(ast.Name, call.func).id
        args = [await self._evaluate(item) for item in call.args]
        kwargs = {
            cast(str, item.arg): await self._evaluate(item.value)
            for item in call.keywords
        }
        if name in self._capability_calls:
            from vibe.core.hardware._geometry import H1_VERSION, resting_geometry

            if len(args) != 1:
                raise self._error(
                    call,
                    "resting_geometry requires one points argument",
                    code="invalid_arguments",
                )
            output = _bounded_json(resting_geometry(args[0], **kwargs), call)
            self._record(
                PolicyStepKind.CAPABILITY,
                name,
                call,
                {"library": H1_VERSION, "output": output},
            )
            return output
        if name in self._direct_action_names:
            if args:
                raise self._error(
                    call,
                    f"Action {name} accepts keyword arguments only",
                    code="invalid_arguments",
                )
            return await self._act(name, kwargs, call)
        if name == "act":
            action = cast(str, args[0])
            return await self._act(action, kwargs, call)
        if name == "observe":
            snapshot = await self._runtime.observe(self._device_id, run_id=self._run_id)
            output = cast(JsonValue, snapshot.model_dump(mode="json"))
            self._record(PolicyStepKind.OBSERVATION, "observe", call, output)
            return output
        if name == "verify":
            criterion = cast(str, args[0])
            report = await self._runtime.verify(
                self._device_id,
                criterion=criterion,
                parameters=_json_mapping(kwargs, call),
                run_id=self._run_id,
            )
            self.last_report = report
            output = cast(JsonValue, report.model_dump(mode="json"))
            self._record(PolicyStepKind.VERIFICATION, criterion, call, output)
            return output
        if name == "stop":
            reason = cast(str, args[0]) if args else "policy requested stop"
            receipt = await self._runtime.stop(
                self._device_id, reason=reason, run_id=self._run_id
            )
            output = cast(JsonValue, receipt.model_dump(mode="json"))
            self._record(PolicyStepKind.STOP, "stop", call, output)
            return output
        if name == "print":
            if len(self.stdout) >= _MAX_STDOUT_LINES:
                raise self._error(
                    call,
                    f"Policy output exceeds {_MAX_STDOUT_LINES} lines",
                    code="output_limit",
                )
            line = " ".join(
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                for item in args
            )
            if len(line) > _MAX_STDOUT_LINE:
                raise self._error(
                    call, "Policy output line is too large", code="output_limit"
                )
            self.stdout.append(line)
            self._record(PolicyStepKind.OUTPUT, "print", call, line)
            return None
        return self._pure_call(name, args, call)

    async def _act(
        self, action: str, parameters: dict[str, Any], node: ast.Call
    ) -> JsonValue:
        if action not in self._action_names:
            raise self._error(
                node, f"Unsupported device action: {action}", code="unknown_action"
            )
        self._action_count += 1
        if self._action_count > self._max_actions:
            raise self._error(
                node,
                f"Policy exceeded {self._max_actions} hardware actions",
                code="action_limit",
            )
        command = HardwareCommand(
            command_id=f"{self._policy_id}-{self._action_count:02d}",
            device_id=self._device_id,
            lease_id=self._lease_id,
            action=action,
            parameters=_json_mapping(parameters, node),
        )
        receipt = await self._runtime.execute(command, run_id=self._run_id)
        self.last_report = None
        self.receipts.append(receipt)
        output = cast(JsonValue, receipt.model_dump(mode="json"))
        self._record(PolicyStepKind.ACTION, action, node, output)
        if receipt.status is not CommandStatus.COMPLETED:
            raise self._error(
                node,
                receipt.error or f"Hardware action {action} did not complete",
                code="action_failed",
            )
        return output

    def _pure_call(self, name: str, args: list[Any], node: ast.AST) -> Any:
        if name == "len" and len(args) == 1:
            return len(args[0])
        if name == "abs" and len(args) == 1:
            return abs(args[0])
        if name == "round" and 1 <= len(args) <= _MAX_ROUND_ARGUMENTS:
            return round(*args)
        if (
            name == "range"
            and 1 <= len(args) <= _MAX_RANGE_ARGUMENTS
            and all(
                isinstance(item, int) and not isinstance(item, bool) for item in args
            )
        ):
            value = range(*args)
            if len(value) > self._max_loop_iterations:
                raise self._error(
                    node,
                    f"range exceeds {self._max_loop_iterations} iterations",
                    code="loop_limit",
                )
            return value
        if name in {"min", "max"} and args:
            function = min if name == "min" else max
            return function(*args)
        if name in {"bool", "float", "int", "str"} and len(args) == 1:
            function = {"bool": bool, "float": float, "int": int, "str": str}[name]
            return function(args[0])
        raise self._error(
            node,
            f"Invalid arguments for policy helper {name}",
            code="invalid_arguments",
        )

    def _binary(
        self, operation: ast.operator, left: Any, right: Any, node: ast.AST
    ) -> Any:
        if isinstance(operation, ast.Add):
            result = left + right
        elif isinstance(operation, ast.Sub):
            result = left - right
        elif isinstance(operation, ast.Mult):
            if not all(isinstance(item, (int, float)) for item in (left, right)):
                raise self._error(
                    node,
                    "Policy multiplication only supports numbers",
                    code="type_error",
                )
            result = left * right
        elif isinstance(operation, ast.Div):
            result = left / right
        elif isinstance(operation, ast.FloorDiv):
            result = left // right
        elif isinstance(operation, ast.Mod):
            result = left % right
        else:  # pragma: no cover - validator owns the closed operator set
            raise self._error(node, "Unsupported binary operator", code="syntax")
        _bounded_json(result, node)
        return result

    @staticmethod
    def _compare(  # noqa: PLR0911
        operation: ast.cmpop, left: Any, right: Any
    ) -> bool:
        if isinstance(operation, ast.Eq):
            return left == right
        if isinstance(operation, ast.NotEq):
            return left != right
        if isinstance(operation, ast.Lt):
            return left < right
        if isinstance(operation, ast.LtE):
            return left <= right
        if isinstance(operation, ast.Gt):
            return left > right
        if isinstance(operation, ast.GtE):
            return left >= right
        if isinstance(operation, ast.In):
            return left in right
        if isinstance(operation, ast.NotIn):
            return left not in right
        raise AssertionError("validator allowed an unsupported comparison")

    def _consume_loop_iteration(self, node: ast.AST) -> None:
        self._loop_iterations += 1
        if self._loop_iterations > self._max_loop_iterations:
            raise self._error(
                node,
                f"Policy exceeded {self._max_loop_iterations} loop iterations",
                code="loop_limit",
            )

    def _record(
        self, kind: PolicyStepKind, name: str, node: ast.Call, output: JsonValue
    ) -> None:
        if len(self.steps) >= _MAX_TRACE_STEPS:
            raise self._error(
                node,
                f"Policy trace exceeds {_MAX_TRACE_STEPS} steps",
                code="trace_limit",
            )
        _bounded_json(output, node)
        self.steps.append(
            HardwarePolicyStep(
                sequence=len(self.steps) + 1,
                kind=kind,
                name=name,
                line=node.lineno,
                output=output,
            )
        )

    @staticmethod
    def _error(node: ast.AST, message: str, *, code: str) -> HardwarePolicyError:
        return HardwarePolicyError(
            message,
            code=code,
            line=getattr(node, "lineno", None),
            column=(getattr(node, "col_offset", None) or 0) + 1,
        )


def _parse_policy(
    source: str,
    *,
    action_names: set[str],
    verification_names: set[str],
    capability_calls: set[str] | None = None,
) -> ast.Module:
    try:
        module = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise HardwarePolicyError(
            exc.msg, code="syntax", line=exc.lineno, column=exc.offset
        ) from exc
    direct_action_names = _direct_action_names(action_names)
    allowed_calls = (
        _PURE_CALLS | _POLICY_CALLS | direct_action_names | (capability_calls or set())
    )
    nodes = list(ast.walk(module))
    if len(nodes) > _MAX_AST_NODES:
        raise HardwarePolicyError(
            "Policy syntax tree is too large", code="policy_too_large"
        )
    for node in nodes:
        if type(node) not in _ALLOWED_NODES:
            raise _validation_error(
                node, f"Policy syntax {type(node).__name__} is not allowed"
            )
        if isinstance(node, ast.Name):
            if node.id.startswith("_") and node.id != "_":
                raise _validation_error(node, "Private names are not allowed")
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise _validation_error(node, "Policy assignment requires one name")
            _validate_assignment_name(node.targets[0], allowed_calls)
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name):
                raise _validation_error(node, "Policy assignment requires one name")
            _validate_assignment_name(node.target, allowed_calls)
        if isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name):
                raise _validation_error(node, "Policy loop target must be one name")
            _validate_assignment_name(node.target, allowed_calls)
        if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
            raise _validation_error(node, "Dictionary unpacking is not allowed")
        if isinstance(node, ast.Call):
            _validate_call(
                node,
                allowed_calls=allowed_calls,
                action_names=action_names,
                direct_action_names=direct_action_names,
                verification_names=verification_names,
            )
    return module


def _direct_action_names(action_names: set[str]) -> set[str]:
    return {
        name
        for name in action_names
        if name.isidentifier() and name not in _PURE_CALLS | _POLICY_CALLS
    }


def _validate_assignment_name(
    node: ast.Name, reserved: set[str] | frozenset[str]
) -> None:
    if node.id in reserved or (node.id.startswith("_") and node.id != "_"):
        raise _validation_error(node, f"Policy cannot assign reserved name: {node.id}")


def _validate_call(
    call: ast.Call,
    *,
    allowed_calls: set[str] | frozenset[str],
    action_names: set[str],
    direct_action_names: set[str],
    verification_names: set[str],
) -> None:
    if not isinstance(call.func, ast.Name) or call.func.id not in allowed_calls:
        raise _validation_error(call, "Policy calls must use an allowed function name")
    if any(item.arg is None for item in call.keywords):
        raise _validation_error(call, "Expanded keyword arguments are not allowed")
    keyword_names = [cast(str, item.arg) for item in call.keywords]
    if len(keyword_names) != len(set(keyword_names)):
        raise _validation_error(call, "Duplicate keyword arguments are not allowed")
    name = call.func.id
    if name in direct_action_names and call.args:
        raise _validation_error(call, f"Action {name} accepts keyword arguments only")
    if name == "act":
        action = _required_literal_string(call, position=0, label="action")
        if len(call.args) != 1 or action not in action_names:
            raise _validation_error(call, f"Unsupported device action: {action}")
    elif name == "observe" and (call.args or call.keywords):
        raise _validation_error(call, "observe() accepts no arguments")
    elif name == "verify":
        criterion = _required_literal_string(call, position=0, label="criterion")
        if len(call.args) != 1 or criterion not in verification_names:
            raise _validation_error(
                call, f"Unsupported verification criterion: {criterion}"
            )
    elif name == "stop":
        if call.keywords or len(call.args) > 1:
            raise _validation_error(call, "stop() accepts at most one reason")
        if call.args:
            _required_literal_string(call, position=0, label="reason")
    elif name == "print" and call.keywords:
        raise _validation_error(call, "print() does not accept keyword arguments")
    elif name in _PURE_CALLS and call.keywords:
        raise _validation_error(
            call, f"Policy helper {name} does not accept keyword arguments"
        )


def _required_literal_string(call: ast.Call, *, position: int, label: str) -> str:
    if len(call.args) <= position:
        raise _validation_error(call, f"Policy call requires a literal {label}")
    value = call.args[position]
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise _validation_error(value, f"Policy {label} must be a literal string")
    return value.value


def _validation_error(node: ast.AST, message: str) -> HardwarePolicyError:
    return HardwarePolicyError(
        message,
        code="unsafe_policy",
        line=getattr(node, "lineno", None),
        column=(getattr(node, "col_offset", None) or 0) + 1,
    )


def _json_mapping(values: dict[str, Any], node: ast.AST) -> dict[str, JsonValue]:
    return {name: _bounded_json(value, node) for name, value in values.items()}


def _bounded_json(value: Any, node: ast.AST) -> JsonValue:
    if isinstance(value, tuple):
        value = list(value)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _validation_error(node, "Policy value must be JSON-compatible") from exc
    if len(encoded) > _MAX_VALUE_BYTES:
        raise _validation_error(node, "Policy value exceeds the 16 KiB limit")
    return cast(JsonValue, value)


__all__ = ["HardwarePolicyError", "HardwarePolicyRunner"]
