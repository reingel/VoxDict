from setuptools import setup, find_packages

setup(
    name="stardict",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "rich>=13.0",
        "readchar>=4.0",
    ],
    entry_points={
        "console_scripts": [
            "stardict=stardict.__main__:main",
        ],
    },
    python_requires=">=3.10",
)
