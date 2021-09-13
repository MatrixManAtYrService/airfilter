import click
from pprint import pformat, pprint
from airframe._version import get_versions


@click.command()
def cli():
    print("cli")


@click.command()
def version():
    pprint(get_versions())
