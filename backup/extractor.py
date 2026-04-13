import zipfile
import os

def extract_twb(file_path, extract_dir="temp"):
    if file_path.endswith(".twb"):
        return file_path

    if not file_path.endswith(".twbx"):
        raise ValueError("Only .twb or .twbx supported")

    if not os.path.exists(extract_dir):
        os.makedirs(extract_dir)

    with zipfile.ZipFile(file_path, 'r') as z:
        for name in z.namelist():
            if name.endswith(".twb"):
                return z.extract(name, extract_dir)

    raise FileNotFoundError("No .twb found inside .twbx")
