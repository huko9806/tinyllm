"""Reads the description stored at the start of a GGUF model file."""
import struct

STRING, ARRAY = 8, 9
FORMATS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def _string(file):
    (length,) = struct.unpack("<Q", file.read(8))
    return file.read(length).decode(errors="replace")


def read_metadata(path):
    """Returns the single values (texts and numbers); lists such as the vocabulary are skipped."""
    values = {}
    with open(path, "rb") as file:
        if file.read(4) != b"GGUF":
            raise ValueError(f"{path} is not a GGUF file")
        file.read(4 + 8)  # version, number of tensors
        (count,) = struct.unpack("<Q", file.read(8))
        for _ in range(count):
            key = _string(file)
            (kind,) = struct.unpack("<I", file.read(4))
            if kind == ARRAY:
                item_kind, length = struct.unpack("<IQ", file.read(12))
                if item_kind == STRING:
                    for _ in range(length):
                        file.seek(struct.unpack("<Q", file.read(8))[0], 1)
                elif item_kind in FORMATS:
                    file.seek(struct.calcsize(FORMATS[item_kind]) * length, 1)
                else:
                    break
            elif kind == STRING:
                values[key] = _string(file)
            elif kind in FORMATS:
                (values[key],) = struct.unpack(FORMATS[kind], file.read(struct.calcsize(FORMATS[kind])))
            else:
                break
    return values
