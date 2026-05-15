import os
import logging
from dotenv import load_dotenv
from pathlib import Path
import json
# Load environment variables
load_dotenv()


def get_env(var_name: str):
    value = os.getenv(var_name)
    if value is None:
        raise EnvironmentError(f"Missing required environment variable: {var_name}")
    return value

# Logging setup
def setup_logger(name="app", log_file="logs/app.log"):
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
# -*- coding: utf-8 -*-

def get_project_root():
    """
    Returns the root folder of the project.
    Works whether running from script or interactive environment.
    """
    try:
        # If running as a script
        return Path(__file__).parent.parent
    except NameError:
        # Fallback for interactive environments (e.g., Jupyter, IPython)
        return Path.cwd()

def load_locations():
    """Returns the locations.json as a list of dictionaries"""
    project_root = get_project_root()
    location_file = project_root / "src" / "locations.json"
    if not location_file.exists():
        raise FileNotFoundError(f"Cannot find {location_file}")
    with location_file.open("r", encoding="utf-8") as f:
        return json.load(f)