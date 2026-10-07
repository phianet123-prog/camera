"""
Command-line user management, for adding accounts without going through
the browser. Usage:

    python manage_users.py add alice
    python manage_users.py remove alice
    python manage_users.py list
"""
import sys
import getpass

import users as user_store


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]

    if action == "add":
        if len(sys.argv) < 3:
            print("Usage: python manage_users.py add <username>")
            return
        username = sys.argv[2]
        password = getpass.getpass(f"Password for '{username}': ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords didn't match.")
            return
        user_store.add_user(username, password)
        print(f"Added user '{username}'.")

    elif action == "remove":
        if len(sys.argv) < 3:
            print("Usage: python manage_users.py remove <username>")
            return
        username = sys.argv[2]
        user_store.remove_user(username)
        print(f"Removed user '{username}' (if it existed).")

    elif action == "list":
        for name in user_store.list_usernames():
            print(f"- {name}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
