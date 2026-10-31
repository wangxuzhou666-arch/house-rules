import os


def save(path, text):
    os.remove(path)
    with open(path, "w") as handle:
        handle.write(text)
