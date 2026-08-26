from setuptools import setup, find_packages, Extension
from Cython.Build import cythonize
import glob
import os

# Get all .py files inside 'iphalakam' (excluding __init__.py)
py_files = (
    [f for f in glob.glob("pnayanam/*.py") if not f.endswith("__init__.py")]
    +
    [f for f in glob.glob("solar_cam/*.py") if not f.endswith("__init__.py")]
)
# Define Cython extensions dynamically
ext_modules = cythonize(
    [
        Extension(
            os.path.splitext(file)[0].replace(os.sep, "."),
            [file],
        )
        for file in py_files
    ],
    compiler_directives={"language_level": "3"},
)

# Define package data with explicit file paths
package_data = {
    'pnayanam': [
        'resources/yolo_models/yolo11n.pt',
        'resources/yolo_models/yolo11m.pt',

        'resources/config.ini',
        'resources/char_dict_path.txt',
        'resources/database_lookup.txt'
    ]
}

setup(
    name='pnayanam',
    version='1.0.0',
    packages=find_packages(),
    ext_modules=ext_modules,
    package_data=package_data,
    include_package_data=True,
    python_requires='>=3.10'
)