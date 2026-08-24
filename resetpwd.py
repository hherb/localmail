from getpass import getpass
import psycopg

from localmail.api.admin.users import set_password
from localmail.config import default_config_path, load_config

password = getpass("New password for horst: ")
confirmation = getpass("Confirm password: ")

if not password:
    raise SystemExit("Password cannot be empty.")
if password != confirmation:
    raise SystemExit("Passwords do not match.")

config = load_config(default_config_path())

with psycopg.connect(config.database.dsn) as connection:
    row = connection.execute(
        "SELECT id FROM api_users WHERE username = %s",
        ("horst",),
    ).fetchone()

    if row is None:
        raise SystemExit("User horst does not exist.")

    set_password(connection, row[0], password)

print("Password updated for horst.")
