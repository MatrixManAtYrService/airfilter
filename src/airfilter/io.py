import io
import logging
import re
import select
import sys
import termios
import textwrap
from collections import deque
from contextlib import redirect_stdout
from dataclasses import dataclass
from typing import Callable

import sh
from InquirerPy import prompt
from InquirerPy.separator import Separator
from sh import ErrorReturnCode, RunningCommand
from sh import bash
from strip_ansi import strip_ansi

# suppress sh's inbuilt logging
# https://stackoverflow.com/questions/56013165/is-it-possible-to-suppress-the-output-of-the-sh-module
logging.getLogger("sh").setLevel(logging.WARNING)

class Args:
    def __init__(self):
        self.verbose = True


class Shim:
    """
    Instead of running the command, pass it to a shim.
    Tests can attach behavior to shims.
    """

    def __init__(self, cmd_regex, handler=lambda: None):
        self.cmd_regex_str = cmd_regex
        self.cmd_regex = re.compile(cmd_regex)
        if callable(handler):
            self.handler = handler
        else:
            self.handler = lambda: handler

    def match(self, cmd_str):
        match_obj = self.cmd_regex.search(cmd_str)
        if match_obj:
            return self.handler, match_obj
        else:
            return False

    def __str__(self):
        return f"<Shim `{self.cmd_regex_str}`>"


# There are probably smarter ways to mock CLI calls
# but I'm on a plane so I'm rolling this one myself
class Shims:
    class Mismatch(Exception):
        pass

    enabled = False
    log = []

    def enable(reset=True):
        Shims.enabled = True
        if reset:
            Shims.shims = deque()
            Shims.log = []

    def disable():
        Shims.enabled = False

    def expect(it):
        try:
            for shim in it:
                Shims.shims.appendleft(shim)
        except TypeError:
            Shims.shims.appendleft(it)

    def run(cmd_str, **kwargs):
        shim = Shims.shims.pop()
        shimpair = shim.match(cmd_str)

        if shimpair:
            handler, match_obj = shimpair
            Shims.log.append(cmd_str)
            try:
                return handler(cmd_str, match=match_obj, **kwargs)
            except TypeError:
                try:
                    return handler(cmd_str, **kwargs)
                except TypeError:
                    return handler(**kwargs)

        else:
            # got an uxpected command, whine about it
            raise Shims.Mismatch(f"Expected {shim} got {cmd_str})")


class PrintContext:
    """
    Keeps track of how indented things are so that printers can do things like this...

    do complexthing
       part one
       part two
       finish up
    done

    ...without explicitly taking an indent-level argument.
    """

    indent = 0

    def __init__(self, cli_args=Args(), logger=None):
        PrintContext.cli_args = cli_args
        PrintContext.logger = logger

    def decrease(self, by=4):
        PrintContext.indent = max(0, self.indent - by)

    def increase(self, by=4):
        PrintContext.indent += by

    def mkstr(self, *args, **kwargs):
        f = io.StringIO()
        with redirect_stdout(f):
            print(*args, **kwargs)
        return textwrap.indent(f.getvalue().strip(), self.indent * " ")


class Info(PrintContext):
    def __call__(self, *args, **kwargs):
        logstr = self.mkstr(*args, **kwargs)
        print(logstr, file=sys.stderr)
        if self.logger:
            self.logger.info(textwrap.dedent(logstr))


class Verbose(PrintContext):
    def __call__(self, *args, **kwargs):
        logstr = self.mkstr(*args, **kwargs)
        if self.cli_args.verbose:
            print(logstr, file=sys.stderr)
        if self.logger:
            self.logger.debug(textwrap.dedent(logstr))


class Silent(PrintContext):
    def __call__(self, *args, **kwargs):
        pass


# groups printer output in an indented chunk with optional header at the top
class Section:
    def __init__(self, printer=Verbose(), header=None):
        self.printer = printer
        if not header:
            self.header = header
        else:
            self.header = str(header)

    def __enter__(self):
        if self.header:
            if self.header[0] == "\n":
                use_header = self.header[1:]
            else:
                use_header = self.header

            self.printer(textwrap.dedent(use_header))
        try:
            self.printer.increase()
        except AttributeError:
            pass  # this is a non-indenting printer

    def __exit__(self, type, value, traceback):
        try:
            self.printer.decrease()
        except AttributeError:
            pass  # this is a non-indenting printer


# because you can't reference exterior classes from a closure, wtf?
_section = Section


def _sectioner(printer):
    """
    syntactic sugar for binding a printer:

        printer = Printer(cli_args)
        header = _sectioner(printer)
        with header("foo"):
            printer("bar")
    """
    _printer = printer

    class BoundSection(_section):
        def __init__(self, header):
            super().__init__(_printer, header=header)

    return BoundSection


def run(
    command,
    runfunc=None,
    printer=Verbose(),
    return_bool=False,
    dedent=True,
    err_on_nonzero=True,
    suppress_output=False,
    line_iterator=False,
    background=False,
    combine_out_err=True,
    workdir=".",
) -> sh.RunningCommand:
    """
    Prints a command, uses sh.py to run it,
    prints its output, returns the result
    """

    if return_bool:
        err_on_nonzero = False
        combine_out_err = False

    # how to run this command?
    def default_runfunc(cmd):
        return bash(["-c", cmd], _err_to_out=combine_out_err, _cwd=workdir)

    def line_iterator_runfunc(cmd):
        return bash(
            ["-c", cmd],
            _err_to_out=True,
            _iter_noblock=True,
            _bg_exc=False,
            _cwd=workdir,
        )

    def background_runfunc(cmd):
        return bash(
            ["-c", cmd],
            _err_to_out=combine_out_err,
            _bg=True,
            _bg_exc=False,
            _cwd=workdir,
        )

    if not runfunc:
        _runfunc = default_runfunc

        if background:
            _runfunc = background_runfunc

        elif line_iterator:
            _runfunc = line_iterator_runfunc

    else:
        _runfunc = runfunc

    if dedent:

        # strip preceeding newline, if exists
        if command[0] == "\n":
            command = command[1:]

        command = textwrap.dedent(command)

    printer("[Command]")
    with Section(printer):
        printer(command)

        result = None
        try:
            if not Shims.enabled:
                result = _runfunc(command)
                returnval = True
            else:
                result = Shims.run(command)
                returnval = True
        except ErrorReturnCode as err:
            result = err
            if err_on_nonzero:
                raise
            elif return_bool:
                returnval = False

    if not (suppress_output or line_iterator or background):
        if result and str(result):
            printer("[Output]")
            with Section(printer):
                printer(result)

    if return_bool:
        return returnval
    else:
        return result


# because you can't def-style functions in a closure, wtf?
_run = run


def _runner(printer, workdir=".") -> Callable[[str], sh.RunningCommand]:
    """
    Bind a printer and a working directory to a command runner
    so it can be dependency-injected into [info/verbose]-agnistic functions
    """
    _printer = printer
    _workdir = workdir

    def bound_runner(*args, **kwargs):
        return _run(*args, printer=_printer, workdir=_workdir, **kwargs)

    return bound_runner


def cmd2str(executed_command):
    """
    Given a recently executed command ()
    Removes ansi control characters and surrounding whitespace
    """
    return strip_ansi(str(executed_command)).strip()


def yes_or_no(question, title=None):
    yes = "Yes"
    no = "No"
    key = "yn"
    answer = prompt(
        {"type": "list", "name": key, "message": question, "choices": [yes, no]}
    )
    return answer[key] == yes


def get_str(prompt_msg, regex=re.compile(".*")):

    key = "str"
    result = prompt(
        {
            "type": "input",
            "name": "str",
            "message": prompt_msg,
            "validate": lambda text: re.match(regex, text),
            "invalid_message": f"must match {regex}",
        }
    )
    return result[key]


def choices(question, _choices, other_choices=None):
    key = "decide"

    def objs(someiter):
        entries = []
        for somestr in set(someiter):
            entries.append({"name": somestr, "value": somestr})
        return entries

    choices = objs(_choices)
    if other_choices:
        choices.append(Separator())
        choices.extend(objs(other_choices))

    answer = prompt(
        {"type": "list", "name": key, "message": question, "choices": choices}
    )
    return answer[key]


@dataclass
class ScopedIO:
    printer: PrintContext
    section: Section
    run: Callable[[str], RunningCommand]


defaults = ScopedIO(
    printer=print, section=_sectioner(printer=print), run=_runner(print)
)

no_output = ScopedIO(
    printer=Silent(), section=_sectioner(printer=Silent()), run=_runner(Silent())
)


class IO:
    """
    Functions for talking to the user,
    or for running commands in a way that is transparent to the user.
    """

    def __init__(self, cli_args: Args, dir: str):
        info_printer = Info(cli_args)
        verbose_printer = Info(cli_args)

        self.info = ScopedIO(
            printer=info_printer,
            section=_sectioner(info_printer),
            run=_runner(info_printer, workdir=dir),
        )
        self.verbose = ScopedIO(
            printer=verbose_printer,
            section=_sectioner(verbose_printer),
            run=_runner(verbose_printer, workdir=dir),
        )


timed_out = "timed_out"


def timeout_prompt(prompt, seconds=5, default=timed_out, io=defaults):
    # https://stackoverflow.com/a/2904057/1054322

    io.printer(f"timeout={seconds}s, defaults_to={default}, prompt=")
    print(f"{prompt} ❯ ", end="", file=sys.stderr)
    sys.stderr.flush()
    i, _, _ = select.select([sys.stdin], [], [], 10)
    if i:
        answer = sys.stdin.readline().strip()
        if not answer:
            return default
        else:
            return answer
    else:
        io.printer(f"\n...timed out, using {default}")
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
        return default
