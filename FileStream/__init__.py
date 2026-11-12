import os
import time
from dotenv import load_dotenv

# Ensure .env is loaded before any submodules are initialized
_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_file = os.path.join(_root_dir, ".env")
if os.path.isfile(_env_file):
    load_dotenv(dotenv_path=_env_file)
elif os.path.isfile(".env"):
    load_dotenv(dotenv_path=".env")

__version__ = "3.1.0"
StartTime = time.time()

