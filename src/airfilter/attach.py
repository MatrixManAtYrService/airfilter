from airfilter.io import IO, Args
import jq
import json


def kube_attach(namespace):
    io = IO(Args(), ".")
    verbose = io.verbose
    with verbose.section("Finding a k8s source of airflow db events..."):

        pods = verbose.run(
            f"kubectl -n {namespace} get pods -o json", suppress_output=True
        ).strip()
        filter = jq.compile(
            '.items[] | select(.metadata.name | contains("postgres")).metadata.name'
        )
        try:
            pod = filter.input(json.loads(pods)).first()
        except StopIteration:
            io.info.printer(pods)
            io.info.printer(filter)
            raise
        verbose.printer(f"found {pod}")

        config = verbose.run(
            f"kubectl -n {namespace} exec {pod} -- cat '/opt/bitnami/postgresql/conf/postgresql.conf'",
            suppress_output=True,
        ).strip()
        if "log_statement = all" not in config:
            verbose.run(
                f"""
                cat <<- 'EOF' | kubectl -n {namespace} exec -i {pod} -- sh -c 'cat - >> /opt/bitnami/postgresql/conf/postgresql.conf'
                log_statement = all
                EOF
                """
            )
        else:
            verbose.printer("logging already enabled")
            with verbose.section("config:"):
                verbose.printer(config)

        verbose.run(
            f"""
            cat <<- 'EOF' | kubectl -n {namespace} exec -i airflow-postgresql-0 -- sh
            PGPASSWORD=postgres psql -U postgres -c "SELECT pg_reload_conf();"
            EOF
            """
        )
