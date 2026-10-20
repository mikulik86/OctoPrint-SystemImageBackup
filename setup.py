# coding=utf-8
import io
import os

from setuptools import setup

here = os.path.abspath(os.path.dirname(__file__))
with io.open(os.path.join(here, "README.md"), encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="OctoPrint-SystemImageBackup",
    version="0.3.1",
    description=(
        "Full, bootable image backups of the Raspberry Pi system (SD card / boot drive) "
        "with image-utils, started from OctoPrint or OctoDash."
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="mikulik86",
    url="https://github.com/mikulik86/OctoPrint-SystemImageBackup",
    license="MIT",
    packages=["octoprint_systemimagebackup"],
    package_data={
        "octoprint_systemimagebackup": [
            "templates/*.jinja2",
            "static/*.html",
            "static/js/*.js",
            "static/css/*.css",
            "helper/*",
        ]
    },
    include_package_data=True,
    zip_safe=False,
    python_requires=">=3.7,<4",
    classifiers=[
        "Framework :: OctoPrint",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
    ],
    entry_points={"octoprint.plugin": ["systemimagebackup = octoprint_systemimagebackup"]},
)
