from tracyy.licensing.client import LicenseClient

client = LicenseClient()
print("CONFIG:")
print(client.config_debug_info())
print(client.config)

result = client.check()
print("\nRESULT:")
print(result)
print("\nRAW SERVER RESPONSE:")
print(client.last_server_response)
print("\nSHEET URL:")
print(
    client.last_server_response.get(
        "sheet_url", "Server không trả sheet_url — deployment đang dùng code cũ."
    )
)
