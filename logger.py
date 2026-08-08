import logging
from logging.handlers import RotatingFileHandler

# Appended to rather than truncated at startup. The log used to be deleted on
# every import, so the one run whose history mattered -- the one that just
# crashed, hung or was killed -- was always the run that had already erased it,
# and /logs could only ever describe the session asking the question. Rotation
# at 5 MB with ten backups already bounds the size, which is what the truncation
# was doing by accident.
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(levelname)s] - %(funcName)s() - Line %(lineno)d: %(name)s - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[
        RotatingFileHandler("logs.txt", mode="a", maxBytes=5000000, backupCount=10),
        logging.StreamHandler(),
    ],
)

logging.getLogger("pyrogram").setLevel(logging.ERROR)


def LOGGER(name: str) -> logging.Logger:
    return logging.getLogger(name)
