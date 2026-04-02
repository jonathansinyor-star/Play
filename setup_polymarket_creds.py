"""
One-time script to generate Polymarket CLOB API credentials from your private key.
Run this once, then copy the output into your .env file.

Usage:
    pip install py-clob-client
    python setup_polymarket_creds.py
"""

import os
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")

if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
    exit(1)

try:
    from py_clob_client.client import ClobClient
except ImportError:
    print("ERROR: py-clob-client not installed. Run: pip install py-clob-client")
    exit(1)

print("Connecting to Polymarket CLOB...")

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137,
)

try:
    # Try to derive existing credentials first (nonce=0)
    creds = client.derive_api_key(nonce=0)
    print("\n✅ Successfully derived existing CLOB API credentials:\n")
except Exception as e:
    print(f"Derive failed ({e}), creating new credentials...")
    try:
        creds = client.create_api_key(nonce=1)
        print("\n✅ Successfully created new CLOB API credentials:\n")
    except Exception as e2:
        print(f"\n❌ Failed to generate credentials: {e2}")
        exit(1)

print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")

# Auto-write to .env
env_path = os.path.join(os.path.dirname(__file__), ".env")
with open(env_path, "r") as f:
    content = f.read()

content = content.replace(
    f"POLYMARKET_API_KEY={os.getenv('POLYMARKET_API_KEY', '')}",
    f"POLYMARKET_API_KEY={creds.api_key}"
)
content = content.replace(
    "POLYMARKET_API_SECRET=",
    f"POLYMARKET_API_SECRET={creds.api_secret}"
)
content = content.replace(
    "POLYMARKET_API_PASSPHRASE=",
    f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}"
)

with open(env_path, "w") as f:
    f.write(content)

print("\n✅ Credentials written to .env automatically.")
print("You can now run: python -m app.main")
