"""Pin the daily release identity before importing application modules."""
import os
os.environ["MIREMOTE_RECOVERY_RELEASE"] = "1"
