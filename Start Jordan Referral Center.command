#!/bin/bash
# Jordan Referral Center — double-click launcher (macOS)
# First run: creates a virtual environment, installs dependencies, sets up
# PostgreSQL in Docker, and loads your data. Every run after that: just
# starts the app and opens it in your browser.

cd "$(dirname "$0")" || exit 1

echo "Jordan Referral Center — starting up..."
echo ""

# 1. Create the virtual environment if it doesn't exist yet
if [ ! -d "venv" ]; then
  echo "First-time setup: creating a Python environment for the app..."
  python3 -m venv venv
  if [ $? -ne 0 ]; then
    echo ""
    echo "Could not create the environment. Make sure Python 3 is installed"
    echo "(python3 --version in Terminal should show a version number)."
    read -p "Press Return to close this window..."
    exit 1
  fi
fi

# 2. Activate it
source venv/bin/activate

# 3. Install dependencies if they're not already there
python3 -c "import flask, reportlab, PIL, psycopg, waitress, apscheduler, dotenv" 2>/dev/null
if [ $? -ne 0 ]; then
  echo "First-time setup: installing dependencies..."
  python3 -m pip install --quiet -r requirements.txt
fi

# 4. Set up PostgreSQL (Docker), schema, and data — every run; each step
#    skips itself automatically once it's already done.
python3 setup.py
if [ $? -ne 0 ]; then
  echo ""
  read -p "Setup did not finish — press Return to close this window..."
  exit 1
fi

# 5. Open the browser shortly after the server starts, then start the server
( sleep 1.5 && open "http://127.0.0.1:5050" ) &

echo ""
echo "Jordan Referral Center is running at http://127.0.0.1:5050"
echo "Leave this window open while you use the app."
echo "Close this window (or press Control-C) to stop it."
echo ""

python3 app.py
