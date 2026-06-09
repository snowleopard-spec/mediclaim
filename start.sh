#!/usr/bin/env bash
# Start MedClaims. First run will need:
#   pip install fastapi "uvicorn[standard]" python-multipart
cd "$(dirname "$0")"
python app.py
