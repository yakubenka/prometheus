"""
Тест баланса Polymarket CLOB
"""
import os, sys

def main():
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder      = os.environ.get("POLYMARKET_FUNDER", "")
    sig_type    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print(f"Funder:    {funder}")
    print(f"Sig type:  {sig_type}")
    print(f"Key prefix: {private_key[:8]}...")

    from py_clob_client.client import ClobClient

    # Test 1: EOA mode (signature_type=0, no funder)
    print("\n--- Test 1: EOA (sig=0, no funder) ---")
    try:
        c = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137, signature_type=0)
        c.set_api_creds(c.create_or_derive_api_creds())
        bal = c.get_balance()
        print(f"Balance: {bal}")
    except Exception as e:
        print(f"Error: {e}")

    # Test 2: EOA with funder
    print("\n--- Test 2: EOA (sig=0, with funder) ---")
    try:
        c = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137, signature_type=0, funder=funder)
        c.set_api_creds(c.create_or_derive_api_creds())
        bal = c.get_balance()
        print(f"Balance: {bal}")
    except Exception as e:
        print(f"Error: {e}")

    # Test 3: Proxy mode (signature_type=1)
    print("\n--- Test 3: Proxy (sig=1, with funder) ---")
    try:
        c = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137, signature_type=1, funder=funder)
        c.set_api_creds(c.create_or_derive_api_creds())
        bal = c.get_balance()
        print(f"Balance: {bal}")
    except Exception as e:
        print(f"Error: {e}")

    # Test 4: Proxy mode (signature_type=2)
    print("\n--- Test 4: Proxy (sig=2, with funder) ---")
    try:
        c = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137, signature_type=2, funder=funder)
        c.set_api_creds(c.create_or_derive_api_creds())
        bal = c.get_balance()
        print(f"Balance: {bal}")
    except Exception as e:
        print(f"Error: {e}")

    print("\nDone.")

if __name__ == "__main__":
    main()
