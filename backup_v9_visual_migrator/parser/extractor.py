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


def extract_images(file_path, output_dir):
    """Extract image files from a TWBX archive.

    Returns a dict mapping the archive path (e.g. 'Image/1s.png') to the
    absolute path where the file was extracted.  Only runs on .twbx files;
    returns an empty dict for plain .twb files.
    """
    if not file_path.endswith(".twbx"):
        return {}

    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg")
    extracted = {}

    with zipfile.ZipFile(file_path, "r") as z:
        for entry in z.namelist():
            if any(entry.lower().endswith(ext) for ext in image_exts):
                # Flatten into images/ dir (avoid nested folders)
                basename = os.path.basename(entry)
                dest = os.path.join(images_dir, basename)
                with z.open(entry) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                extracted[entry] = os.path.abspath(dest)

    return extracted
