"""Fake CLI for lint fixtures."""
import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="notme")
    sub = parser.add_subparsers(dest="command")
    for name in ("config", "doctor", "claim-issue"):
        sub.add_parser(name)
    return parser
