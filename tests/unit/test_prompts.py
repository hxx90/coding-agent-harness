from agent_harness.prompts import BASE_SYSTEM_PROMPT


def test_prompt_distinguishes_read_only_and_sensitive_paths():
    assert "可以读取只读保护路径" in BASE_SYSTEM_PROMPT
    assert "但不得修改" in BASE_SYSTEM_PROMPT
    assert "不得访问 Workspace 外部或读写敏感路径" in BASE_SYSTEM_PROMPT
