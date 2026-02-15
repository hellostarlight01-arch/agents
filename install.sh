#!/bin/bash
set -e

echo "Installing dependencies..."

# Install safe-pysha3 first (compatible fork for Python 3.11+)
pip install safe-pysha3==1.0.4

# Create a dummy pysha3 package so pip won't try to build the broken one
mkdir -p /tmp/pysha3_shim
cat > /tmp/pysha3_shim/setup.py << 'PYSETUP'
from setuptools import setup
setup(name='pysha3', version='1.0.2', py_modules=[])
PYSETUP
pip install /tmp/pysha3_shim/ 2>/dev/null
rm -rf /tmp/pysha3_shim

# Install all requirements (excluding pysha3 since we already handled it)
grep -v "pysha3" requirements.txt > /tmp/req_clean.txt
pip install -r /tmp/req_clean.txt
rm /tmp/req_clean.txt

echo ""
echo "All dependencies installed successfully!"
