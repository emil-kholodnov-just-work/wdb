# *-* coding: utf-8 *-*
from ._compat import (
    loads, dumps, JSONEncoder, quote, execute, to_unicode, u, StringIO, escape,
    to_unicode_string, from_bytes, force_bytes)
from .utils import get_source, get_doc, executable_line, importable_module
from . import __version__, _initial_globals
from tokenize import generate_tokens, TokenError
from difflib import HtmlDiff
import datadiff
from datadiff import DiffNotImplementedForType
import token as tokens
from jedi import Interpreter
from logging import getLogger
from shutil import move
from tempfile import gettempdir
from base64 import b64encode

try:
    from cutter import cut
    from cutter.utils import bang_compile as compile
except ImportError:
    cut = None

try:
    import magic
except ImportError:
    magic = None

import os
import re
import sys
import time
import traceback
log = getLogger('wdb.ui')


class ReprEncoder(JSONEncoder):
    """JSON encoder using repr for objects"""

    def default(self, obj):
        return repr(obj)


def dump(o):
    """Shortcut to json.dumps with ReprEncoder"""
    return dumps(o, cls=ReprEncoder, sort_keys=True)


def tokenize_redir(raw_data):
    raw_io = StringIO()
    raw_io.write(raw_data)
    raw_io.seek(0)
    last_token = ''

    for token_type, token, src, erc, line in generate_tokens(raw_io.readline):
        if (token_type == tokens.ERRORTOKEN and
                token == '!' and
                last_token in ('>', '>>')):
            return (line[:src[1] - 1],
                    line[erc[1]:].lstrip(),
                    last_token == '>>')
        last_token = token
    return


class Interaction(object):

    hooks = {
        'update_watchers': [
            'start', 'eval', 'watch', 'init', 'select', 'unwatch']
    }

    def __init__(
            self, db, frame, tb, exception, exception_description,
            init=None, parent=None, shell=False):
        self.db = db
        self.parent = parent
        self.shell = shell
        self.init_message = init
        self.stack, self.trace, self.index = self.db.get_trace(frame, tb)
        self.exception = exception
        self.exception_description = exception_description
        # Copy locals to avoid strange cpython behaviour
        self.locals = list(map(lambda x: x[0].f_locals, self.stack))
        self.htmldiff = HtmlDiff()
        if self.shell:
            self.locals[self.index] = {}

    def hook(self, kind):
        for hook, events in self.hooks.items():
            if kind in events:
                getattr(self, hook)()

    @property
    def current(self):
        return self.trace[self.index]

    @property
    def current_frame(self):
        return self.stack[self.index][0]

    @property
    def current_locals(self):
        return self.locals[self.index]

    @property
    def current_file(self):
        return self.current['file']

    def get_globals(self):
        """Get enriched globals"""
        if self.shell:
            globals_ = dict(_initial_globals)
        else:
            globals_ = dict(self.current_frame.f_globals)
        globals_['_'] = self.db.last_obj
        if cut is not None:
            globals_['cut'] = cut
        # For meta debuging purpose
        globals_['___wdb'] = self.db
        # Hack for function scope eval
        globals_.update(self.current_locals)
        for var, val in self.db.extra_vars.items():
            globals_[var] = val
        self.db.extra_items = {}
        return globals_

    def init(self):
        self.db.send('Title|%s' % dump({
            'title': self.exception,
            'subtitle': self.exception_description
        }))
        if self.shell:
            self.db.send('Shell')
        else:
            self.db.send('Trace|%s' % dump({
                'trace': self.trace,
                'cwd': os.getcwd()
            }))
            self.db.send('SelectCheck|%s' % dump({
                'frame': self.current,
                'name': self.current_file
            }))
        if self.init_message:
            self.db.send(self.init_message)
            self.init_message = None
        self.hook('init')

    def parse_command(self, message):
        # Parse received message
        if '|' in message:
            return message.split('|', 1)
        return message, ''

    def loop(self):
        stop = False
        while not stop:
            try:
                stop = self.interact()
            except Exception:
                log.exception('Error in loop')
                try:
                    exc = self.handle_exc()
                    type_, value = sys.exc_info()[:2]
                    link = (
                        '<a href="https://github.com/Kozea/wdb/issues/new?'
                        'title=%s&body=%s&labels=defect" class="nogood">'
                        'Please click here to report it on Github</a>') % (
                        quote('%s: %s' % (type_.__name__, str(value))),
                        quote('```\n%s\n```\n' %
                              traceback.format_exc()))
                    self.db.send('Echo|%s' % dump({
                        'for': 'Error in Wdb, this is bad',
                        'val': exc + '<br>' + link
                    }))
                except Exception:
                    log.exception('Error in loop exception handling')
                    self.db.send('Echo|%s' % dump({
                        'for': 'Too many errors',
                        'val': ("Don't really know what to say. "
                                "Maybe it will work tomorrow.")
                    }))

    def interact(self):
        try:
            message = self.db.receive()
        except KeyboardInterrupt:
            # Quit on KeyboardInterrupt
            message = 'Quit'

        cmd, data = self.parse_command(message)
        cmd = cmd.lower()
        log.debug('Cmd %s #Data %d' % (cmd, len(data)))
        fun = getattr(self, 'do_' + cmd, None)
        if fun:
            rv = fun(data)
            self.hook(cmd)
            return rv

        log.warning('Unknown command %s' % cmd)

    def update_watchers(self):
        watched = {}
        for watcher in self.db.watchers[self.current_file]:
            try:
                watched[watcher] = self.db.safe_better_repr(eval(
                    watcher, self.get_globals(), self.locals[self.index]))
            except Exception as e:
                watched[watcher] = type(e).__name__

        self.db.send('Watched|%s' % dump(watched))

    def notify_exc(self, msg):
        log.info(msg, exc_info=True)
        self.db.send('Log|%s' % dump({
            'message': '%s\n%s' % (msg, traceback.format_exc())
        }))

    def do_start(self, data):
        # Getting breakpoints
        log.debug('Getting breakpoints')

        self.db.send('Init|%s' % dump({
            'cwd': os.getcwd(),
            'version': __version__,
            'breaks': self.db.breakpoints_to_json()
        }))
        self.db.send('Title|%s' % dump({
            'title': self.exception,
            'subtitle': self.exception_description
        }))
        if self.shell:
            self.db.send('Shell')
        else:
            self.db.send('Trace|%s' % dump({
                'trace': self.trace
            }))

            # In case of exception always be at top frame to start
            self.index = len(self.stack) - 1
            self.db.send('SelectCheck|%s' % dump({
                'frame': self.current,
                'name': self.current_file
            }))

        if self.init_message:
            self.db.send(self.init_message)
            self.init_message = None

    def do_select(self, data):
        self.index = int(data)
        self.db.send('SelectCheck|%s' % dump({
            'frame': self.current,
            'name': self.current_file
        }))

    def do_file(self, data):
        fn = data
        file = self.db.get_file(fn)
        self.db.send('Select|%s' % dump({
            'frame': self.current,
            'name': fn,
            'file': file
        }))

    def do_inspect(self, data):
        try:
            thing = self.db.obj_cache.get(int(data))
        except Exception:
            self.fail('Inspect')
            return

        if (isinstance(thing, tuple) and len(thing) == 3):
            type_, value, tb = thing
            iter_tb = tb
            while iter_tb.tb_next != None:
                iter_tb = iter_tb.tb_next

            interaction = Interaction(
                self.db, iter_tb.tb_frame, tb,
                'RECURSIVE %s' % type_.__name__,
                str(value), parent=self
            )
            interaction.init()
            interaction.loop()
            self.init()
            return

        self.db.send('Dump|%s' % dump({
            'for': repr(thing),
            'val': self.db.dmp(thing),
            'doc': get_doc(thing),
            'source': get_source(thing)
        }))

    def do_dump(self, data):
        try:
            thing = eval(
                data, self.get_globals(), self.locals[self.index])
        except Exception:
            self.fail('Dump')
            return

        self.db.send('Dump|%s' % dump({
            'for': u('%s ⟶ %s ') % (data, repr(thing)),
            'val': self.db.dmp(thing),
            'doc': get_doc(thing),
            'source': get_source(thing)}))

    def do_trace(self, data):
        self.db.send('Trace|%s' % dump({
            'trace': self.trace
        }))

    def do_eval(self, data):
        redir = None
        suggest = None
        raw_data = data.strip()
        if raw_data.startswith('!<'):
            filename = raw_data[2:].strip()
            try:
                with open(filename, 'r') as f:
                    raw_data = f.read()
            except Exception:
                self.fail('Eval', 'Unable to read from file %s' % filename)
                return

        lines = raw_data.split('\n')
        if '>!' in lines[-1]:
            try:
                last_line, redir, append = tokenize_redir(raw_data)
            except TokenError:
                last_line = redir = None
                append = False
            if redir and last_line:
                indent = len(lines[-1]) - len(lines[-1].lstrip())
                lines[-1] = indent * u(' ') + last_line
                raw_data = '\n'.join(lines)
        data = raw_data
        # Keep spaces
        raw_data = raw_data.replace(' ', u(' '))
        # Compensate prompt for multi line
        raw_data = raw_data.replace('\n', '\n' + u(' ' * 4))
        duration = None
        with self.db.capture_output(
                with_hook=redir is None) as (out, err):
            try:
                compiled_code = compile(data, '<stdin>', 'single')
                self.db.compile_cache[id(compiled_code)] = data

                l = self.locals[self.index]
                start = time.time()
                execute(compiled_code, self.get_globals(), l)
                duration = int((time.time() - start) * 1000 * 1000)
            except NameError as e:
                m = re.match("name '(.+)' is not defined", str(e))
                if m:
                    name = m.groups()[0]
                    if importable_module(name):
                        suggest = 'import %s' % name

                self.db.hooked = self.handle_exc()
            except Exception:
                self.db.hooked = self.handle_exc()

        if redir and not self.db.hooked:
            try:
                with open(redir, 'a' if append else 'w') as f:
                    f.write('\n'.join(out) + '\n'.join(err) + '\n')
            except Exception:
                self.fail('Eval', 'Unable to write to file %s' % redir)
                return
            self.db.send('Print|%s' % dump({
                'for': raw_data,
                'result': escape('%s to file %s' % (
                    'Appended' if append else 'Written', redir),)
            }))
        else:
            rv = escape('\n'.join(out) + '\n'.join(err))
            try:
                _ = dump(rv)
            except Exception:
                rv = rv.decode('ascii', 'ignore')

            if rv and self.db.last_obj is None or not self.db.hooked:
                result = rv
            elif not rv:
                result = self.db.hooked
            else:
                result = self.db.hooked + '\n' + rv

            self.db.send('Print|%s' % dump({
                'for': raw_data,
                'result': result,
                'suggest': suggest,
                'duration': duration
            }))

    def do_ping(self, data):
        self.db.send('Pong')

    def do_step(self, data):
        self.db.set_step(self.current_frame)
        return True

    def do_next(self, data):
        self.db.set_next(self.current_frame)
        return True

    def do_continue(self, data):
        self.db.stepping = False
        self.db.set_continue(self.current_frame)
        return True

    def do_return(self, data):
        self.db.set_return(self.current_frame)
        return True

    def do_until(self, data):
        self.db.set_until(self.current_frame)
        return True

    def do_break(self, data):
        from linecache import getline

        brk = loads(data)
        break_fail = lambda x: self.fail(
            'Break', 'Break on %s failed' % (
                '%s:%s' % (brk['fn'], brk['lno'])), message=x)

        if brk['lno'] is not None:
            try:
                lno = int(brk['lno'])
            except Exception:
                break_fail(
                    'Wrong breakpoint format must be '
                    '[file][:lineno][#function][,condition].')
                return

            line = getline(
                brk['fn'], lno, self.current_frame.f_globals)
            if not line:
                for path in sys.path:
                    line = getline(
                        os.path.join(path, brk['fn']),
                        brk['lno'], self.current_frame.f_globals)
                    if line:
                        break
            if not line:
                break_fail('Line does not exist')
                return

            if not executable_line(line):
                break_fail('Blank line or comment')
                return

        breakpoint = self.db.set_break(
            brk['fn'], brk['lno'], brk['temporary'], brk['cond'], brk['fun'])
        break_set = breakpoint.to_dict()
        break_set['temporary'] = brk['temporary']
        self.db.send('BreakSet|%s' % dump(break_set))

    def do_unbreak(self, data):
        brk = loads(data)
        lno = brk['lno'] and int(brk['lno'])
        self.db.clear_break(
            brk['fn'], lno, brk['temporary'], brk['cond'], brk['fun'])

        self.db.send('BreakUnset|%s' % data)

    def do_breakpoints(self, data):
        self.db.send('Print|%s' % dump({
            'for': 'Breakpoints',
            'result': self.db.breakpoints
        }))

    def do_watch(self, data):
        self.db.watchers[self.current_file].append(data)
        self.db.send('Ack')

    def do_unwatch(self, data):
        self.db.watchers[self.current_file].remove(data)

    def do_jump(self, data):
        lno = int(data)
        if self.index != len(self.trace) - 1:
            log.error('Must be at bottom frame')
            return

        try:
            self.current_frame.f_lineno = lno
        except ValueError:
            self.fail('Unbreak')
            return

        self.current['lno'] = lno
        self.db.send('Trace|%s' % dump({
            'trace': self.trace
        }))
        self.db.send('SelectCheck|%s' % dump({
            'frame': self.current,
            'name': self.current_file
        }))

    def do_complete(self, data):
        script = Interpreter(data, [self.current_locals, self.get_globals()])
        try:
            completions = script.completions()
        except Exception:
            self.db.send('Suggest')
            self.notify_exc('Completion failed for %s' % data)
            return

        try:
            funs = script.call_signatures() or []
        except Exception:
            self.db.send('Suggest')
            self.notify_exc('Completion of function failed for %s' % data)
            return

        try:
            suggest_obj = {
                'params': [{
                    'params': [p.get_code().replace('\n', '')
                               for p in fun.params],
                    'index': fun.index,
                    'module': fun.module_name,
                    'call_name': fun.name} for fun in funs],
                'completions': [{
                    'base': comp.name[
                        :len(comp.name) - len(comp.complete)],
                    'complete': comp.complete,
                    'description': comp.description
                } for comp in completions if comp.name.endswith(
                    comp.complete)]
            }
            self.db.send('Suggest|%s' % dump(suggest_obj))
        except Exception:
            self.db.send('Suggest')
            self.notify_exc('Completion generation failed for %s' % data)

    def do_save(self, data):
        fn, src = data.split('|', 1)
        if os.path.exists(fn):
            dn = os.path.dirname(fn)
            bn = os.path.basename(fn)
            try:
                move(
                    fn, os.path.join(
                        gettempdir(),
                        dn.replace(os.path.sep, '!') + bn +
                        '-wdb-back-%d' % time.time()))
                with open(fn, 'w') as f:
                    f.write(to_unicode_string(src, fn))
            except OSError as e:
                self.db.send('Echo|%s' % dump({
                    'for': 'Error during save',
                    'val': str(e)
                }))
            else:
                self.db.send('Echo|%s' % dump({
                    'for': 'Save succesful',
                    'val': 'Wrote %s' % fn
                }))

    def do_display(self, data):
        if ';' in data:
            mime, data = data.split(';', 1)
            forced = True
        else:
            mime = 'text/html'
            forced = False

        try:
            thing = eval(
                data, self.get_globals(), self.locals[self.index])
        except Exception:
            self.fail('Display')
            return
        else:
            thing = force_bytes(thing)
            if magic and not forced:
                with magic.Magic(flags=magic.MAGIC_MIME_TYPE) as m:
                    mime = m.id_buffer(thing)
            self.db.send('Display|%s' % dump({
                'for': u('%s (%s)') % (data, mime),
                'val': from_bytes(b64encode(thing)),
                'type': mime}))

    def do_disable(self, data):
        self.db.__class__.enabled = False
        self.db.stepping = False
        self.db.stop_trace()
        self.db.die()
        return True

    def do_quit(self, data):
        self.db.stepping = False
        self.db.stop_trace()
        sys.exit(1)

    def do_diff(self, data):
        split = data.split('!') if '!' in data else data.split('<>')
        file1, file2 = map(
            lambda x: eval(x, self.get_globals(), self.locals[self.index]),
            split)
        try:
            file1, file2 = str(file1), str(file2)
        except TypeError:
            self.fail('Diff', title='TypeError',
                      message='Strings are expected as input.')
            return
        self.db.send('RawHTML|%s' % dump({
            'for': u('Difference between %s') % (data),
            'val': self.htmldiff.make_file([file1],  [file2])}))

    def do_structureddiff(self, data):
        split = data.split('!') if '!' in data else data.split('<>')
        left_struct, right_struct = map(
            lambda x: eval(x, self.get_globals(), self.locals[self.index]),
            split)
        try:
            datadiff.diff(left_struct, right_struct)
        except DiffNotImplementedForType:
            self.fail('StructuredDiff',
                      title='TypeError', message='A structure was expected')
            return
        self.db.send('Echo|%s' % dump({
            'for': u('Difference of structures %s' % data),
            'val': (datadiff.diff(left_struct, right_struct).stringify()
                    .replace('\n', '<br />')),
            'mode': 'diff'}))

    def handle_exc(self):
        """Return a formated exception traceback for wdb.js use"""
        exc_info = sys.exc_info()
        type_, value = exc_info[:2]
        self.db.obj_cache[id(exc_info)] = exc_info

        return '<a href="%d" class="inspect">%s: %s</a>' % (
            id(exc_info),
            escape(type_.__name__), escape(str(value)))

    def fail(self, cmd, title=None, message=None):
        """Send back captured exceptions"""
        if message is None:
            message = self.handle_exc()
        else:
            message = escape(message)
        self.db.send('Echo|%s' % dump({
            'for': escape(title or '%s failed' % cmd),
            'val': message
        }))
