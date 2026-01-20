#!/bin/bash
cd /home/sgadmin/services/spirrow/spirrow-magickit
source venv/bin/activate
export MAGICKIT_CONFIG=/home/sgadmin/services/spirrow/spirrow-magickit/config/magickit_config.yaml
exec python -m uvicorn magickit.main:app --host 0.0.0.0 --port 8113
