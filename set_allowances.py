"""
Polymarket — Set Allowances (one-time setup)
Запускается один раз чтобы дать Polymarket разрешение на использование USDC.
"""
import os
import sys

def main():
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    public_key  = os.environ.get("POLYMARKET_FUNDER", "")

    if not private_key or not public_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER must be set")
        sys.exit(1)

    print(f"Setting allowances for wallet: {public_key[:10]}...")

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
            signature_type=0,
        )

        print("Setting allowances...")
        resp = client.set_allowances()
        print(f"Allowances set: {resp}")
        print("SUCCESS - you can now trade!")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
