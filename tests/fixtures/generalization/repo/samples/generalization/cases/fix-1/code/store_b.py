import os


def save(path, text):
    scratch = path + ".new"
    with open(scratch, "w") as handle:
        handle.write(text)
    os.replace(scratch, path)
