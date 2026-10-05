"""Prepare the verified official .NET 8 SDK inside this project."""

import hashlib
import urllib.request
import zipfile
from pathlib import Path

SDK_VERSION = "8.0.425"
SDK_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/Sdk/"
    f"{SDK_VERSION}/dotnet-sdk-{SDK_VERSION}-win-x64.zip"
)
SDK_SHA512 = (
    "f0b6f15bf6f1a0507205c0cb102ab99e1dee875c4682c8ed94665be1d580186a06"
    "b21455e83b3a01a0ff7f4cd887b67420f2e2fe09ed985534a4cea488ae1af9"
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / ".tools" / "dotnet"
    if (destination / "sdk" / SDK_VERSION / "dotnet.dll").is_file():
        print(f".NET SDK {SDK_VERSION} is already prepared.")
        return

    cache = root / ".cache"
    cache.mkdir(exist_ok=True)
    archive = cache / f"dotnet-sdk-{SDK_VERSION}-win-x64.zip"
    if not archive.is_file():
        print(f"Downloading official .NET SDK {SDK_VERSION}...", flush=True)
        with urllib.request.urlopen(SDK_URL, timeout=60) as response:
            with archive.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)

    with archive.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha512").hexdigest()
    if checksum != SDK_SHA512:
        archive.unlink()
        raise RuntimeError(".NET SDK checksum mismatch; retry setup.")

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            if (
                not (destination / entry.filename)
                .resolve()
                .is_relative_to(destination.resolve())
            ):
                raise RuntimeError("Unexpected SDK archive path.")
        package.extractall(destination)
    print(f".NET SDK {SDK_VERSION} is ready.")


if __name__ == "__main__":
    main()
