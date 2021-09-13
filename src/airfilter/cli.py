import click
from pprint import pformat, pprint
from airfilter._version import get_versions
from airfilter.attach import kube_attach


@click.command()
@click.argument("namespace")
def cli(namespace):
    kube_attach(namespace)


@click.command()
def version():
    pprint(get_versions())
