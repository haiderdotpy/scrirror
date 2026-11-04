from adb_setup import phone_link
from virtual_monitor import add_virtual_monitor


def main():
    with phone_link(8080):
        add_virtual_monitor()

if __name__ == "__main__":
    main()
