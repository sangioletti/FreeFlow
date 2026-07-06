"""Minimal setup.py for FlowCyt."""

from setuptools import setup, find_packages

setup(
    name="flowcyt",
    version="1.0.0",
    description="FCS flow-cytometry file viewer and gating tool",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "numpy>=1.22",
        "matplotlib>=3.5",
        "requests>=2.28",
    ],
    entry_points={
        "console_scripts": [
            "flowcyt=flowcyt.cli:main",
        ],
    },
)
