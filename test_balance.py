"""
Тест баланса - v3
"""
import os, requests as req

def main():
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder      = os.environ.get("POLYMARKET_FUNDER", "")
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    print(f"Funder: {funder}")

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    # Step 1: Get creds using EOA only (no funder)
    print("\n--- Getting API creds ---")
    c0 = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137)
    try:
        creds = c0.create_or_derive_api_creds()
        print(f"Creds type: {type(creds)}")
        print(f"Creds: {creds}")
    except Exception as e:
        print(f"Creds error: {e}")
        creds = None

    # Step 2: Use creds with funder
    print("\n--- Testing with creds + funder ---")
    try:
        c1 = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137,
                       signature_type=0, funder=funder)
        if creds:
            c1.set_api_creds(creds)
        bal = c1.get_balance_allowance(params={"asset_type": "USDC", "signature_type": 0})
        print(f"Balance: {bal}")
    except Exception as e:
        print(f"Error: {e}")

    # Step 3: Direct HTTP with L1 auth
    print("\n--- Direct HTTP ---")
    try:
        import time
        from py_clob_client.signing.hmac import create_l1_headers
        headers = create_l1_headers(private_key, "GET", "/balance-allowance", None, int(time.time()))
        r = req.get("https://clob.polymarket.com/balance-allowance",
                   params={"asset_type": "USDC", "signature_type": "0"},
                   headers=headers, timeout=10)
        print(f"Status: {r.status_code} | {r.text[:300]}")
    except Exception as e:
        print(f"HTTP error: {e}")

    print("\nDone.")

if __name__ == "__main__":
    main()
