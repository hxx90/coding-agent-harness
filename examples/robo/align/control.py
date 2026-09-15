# Local visual feedback: raw pixels -> centroid -> bounded correction.
# `robo` is the SDK injected by the frozen execution worker.
import math

stable = 0
for step in range(70):
    scene = robo.observe()
    x, y = robo.centroid(scene)
    error = math.hypot(x - 32, y - 32)
    print("step", step, "pixel error", error)
    if error <= 0.5:
        stable += 1
        if stable >= 5:
            break
    else:
        stable = 0
        gain = robo.parameters["gain"]
        dx = max(-4, min(4, (32 - x) * gain))
        dy = max(-4, min(4, (32 - y) * gain))
        command = "correction-" + str(step)
        result = robo.move(dx, dy, based_on=scene["id"], command_id=command,
                           lose_ack=(step == 0))
        if result["status"] == "unknown":
            result = robo.query(command)
            if result["status"] != "confirmed":
                raise RuntimeError("Motion remains unknown; stopping for inspection")
    robo.sleep(0.03)
else:
    raise RuntimeError("Local controller did not converge")
print("Local loop complete; the host will independently observe the result.")
