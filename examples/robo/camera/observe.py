"""Local perception on real camera pixels; no model calls in the frame loop."""
import base64
import json

version = 1
for index in range(int(robo.parameters["frames"])):
    observation = robo.observe()
    pixels = base64.b64decode(observation["pixels"]["rgb"])
    mean = round(sum(pixels) / len(pixels), 2)
    robo.log(json.dumps({
        "version": version, "index": index, "observation": observation["id"],
        "capture_seq": observation["capture_seq"], "sampled_at": observation["sampled_at"],
        "mean_channel_value": mean, "dark_frame": mean < 15,
    }))
    robo.sleep(robo.parameters["interval"])
robo.log("Camera sampling complete; scene-goal verification remains independent.")
