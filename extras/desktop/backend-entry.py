"""Boot the bundled API without loading a developer's environment file."""
import os
from pathlib import Path

from novelvideo.doubao_work import client

client.GATEWAY_PROJECT = os.environ["QJIN_DOUBAO_PROJECT"]

import uvicorn
from novelvideo.api.app import app

for directory in ("state", "output", "runtime"):
    Path(os.environ["NOVELVIDEO_DATA_ROOT"], directory).mkdir(parents=True, exist_ok=True)

uvicorn.run(app, host="127.0.0.1", port=int(os.environ["NOVELVIDEO_API_PORT"]), log_level="info")
