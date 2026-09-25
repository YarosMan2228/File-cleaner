"""«File Cleaner.exe»: без аргументов открывает окно программы, с аргументами — как filecleaner."""
import sys

from filecleaner.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["gui"]))
