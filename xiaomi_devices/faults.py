"""Fault codes and what they mean, so a caller gets a sentence instead of a number.

A bare integer tells whoever reads the log nothing. The table below maps the device's own
error codes to a description and a subsystem, so a report can say "the robot reports it is
stuck or trapped (code 32, navigation)" rather than "error 32".

Codes and messages follow the upstream Valetudo Dreame implementation, which is the reference
for this device family. The message text is quoted from there; the ``subsystem`` grouping is
ours and exists so a caller can tell a navigation problem from a hardware one.

A note on judgement: this module describes WHAT the device reports. It does not grade how
serious a fault is or whether it is likely to clear, because that cannot be known from the
code alone — the same code can arise from a momentary obstruction or from a broken part, and
only the physical situation distinguishes them. Report the code, describe the condition, and
ask. Do not tell someone a fault is benign.
"""

# code -> (message, subsystem)
FAULTS = {
    1: ("Wheel lost floor contact", "wheel"),
    2: ("Cliff sensor dirty or robot on the verge of falling", "sensor"),
    3: ("Stuck front bumper", "bumper"),
    4: ("Tilted robot", "attitude"),
    5: ("Stuck front bumper", "bumper"),
    6: ("Wheel lost floor contact", "wheel"),
    7: ("Internal error", "internal"),
    8: ("Dustbin missing", "consumable"),
    9: ("Water tank missing", "consumable"),
    10: ("Water tank empty", "consumable"),
    11: ("Dustbin full", "consumable"),
    12: ("Main brush jammed", "brush"),
    13: ("Side brush jammed", "brush"),
    14: ("Filter jammed", "filter"),
    15: ("Robot stuck or trapped", "navigation"),
    16: ("Robot stuck or trapped", "navigation"),
    17: ("Robot stuck or trapped", "navigation"),
    18: ("Robot stuck or trapped", "navigation"),
    19: ("Charging station without power", "power"),
    20: ("Low battery", "battery"),
    21: ("Charging error", "power"),
    23: ("Internal error", "internal"),
    24: ("Camera dirty", "sensor"),
    25: ("Internal error", "internal"),
    26: ("Camera dirty", "sensor"),
    27: ("Sensor dirty", "sensor"),
    28: ("Charging station without power", "power"),
    29: ("Battery temperature out of operating range", "battery"),
    30: ("Fan speed abnormal", "fan"),
    31: ("Robot stuck or trapped", "navigation"),
    32: ("Robot stuck or trapped", "navigation"),
    33: ("Accelerometer sensor error", "sensor"),
    34: ("Gyroscope sensor error", "sensor"),
    35: ("Gyroscope sensor error", "sensor"),
    36: ("Left magnetic field sensor error", "sensor"),
    37: ("Right magnetic field sensor error", "sensor"),
    38: ("Internal error", "internal"),
    39: ("Internal error", "internal"),
    40: ("Camera fault", "sensor"),
    41: ("Magnetic interference", "environment"),
    42: ("Water pump fault", "pump"),
    43: ("RTC fault", "internal"),
    44: ("Internal error", "internal"),
    45: ("3.3V rail abnormal", "power"),
    46: ("Internal error", "internal"),
    47: ("Cannot reach target", "navigation"),
    48: ("LDS jammed", "sensor"),
    49: ("LDS bumper jammed", "sensor"),
    50: ("Internal error", "internal"),
    51: ("Filter jammed", "filter"),
    52: ("Internal error", "internal"),
    53: ("ToF Sensor offline", "sensor"),
    54: ("Wall sensor dirty", "sensor"),
    55: ("Attempted to start mopping while on carpet", "environment"),
    56: ("Internal error", "internal"),
    57: ("Internal error", "internal"),
}

# Codes worth naming explicitly for callers that branch on them.
NO_FAULT = 0
STUCK_OR_TRAPPED = 32


def describe(raw):
    """Turn a raw fault code into a dict, or None when there is no fault.

    ``None`` means the device reports no fault — it does not mean the device is healthy.
    """
    if raw in (NO_FAULT, "0", "", None):
        return None
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return {"code": raw, "message": "unrecognised fault value", "subsystem": "unknown"}

    message, subsystem = FAULTS.get(code, (f"Unknown fault {code}", "unknown"))
    return {"code": code, "message": message, "subsystem": subsystem}
