# -*- coding: utf-8 -*-
import re
import sublime
from sublime_plugin import TextCommand, WindowCommand

from SublimeBoo import query_members, server, get_code
from BooHints import format_type, format_method


MEMBER_REGEX = re.compile(r'[\w\)\]]\.$')
WORD_REGEX = re.compile(r'^\w+$')


class BooDotCompleteCommand(TextCommand):
    """
    Command triggered when the dot key is pressed to show the autocomplete popup
    """
    def run(self, edit):
        # Insert the dot in the buffer
        for region in self.view.sel():
            self.view.insert(edit, region.end(), ".")

        # Trigger auto complete
        caret = self.view.sel()[0].begin()
        line = self.view.substr(sublime.Region(self.view.word(caret - 1).a, caret))
        if MEMBER_REGEX.search(line) is not None:
            self.view.run_command('hide_auto_complete')
            sublime.set_timeout(self.delayed_complete, 0)

    def delayed_complete(self):
        self.view.run_command("auto_complete")


class BooQuickPanelCompleteCommand(TextCommand):

    def run(self, edit):
        view = self.view

        # Find a preceding non-word character in the line
        offset = view.sel()[0].a
        self.word = view.substr(view.word(offset)).rstrip()
        if self.word.isalnum():
            self.word = self.word.lower()
            offset -= len(self.word)
        else:
            self.word = ''

        # TODO: Obtain proper hints like we do on dot
        # TODO: Work with entities so we can offer good signatures

        resp = server(view).query(
            'members',
            fname=view.file_name(),
            code=get_code(view),
            offset=offset,
            extra=True)
        hints = resp['hints']

        if view.substr(offset-1) != '.':
            resp = server(view).query(
                'globals',
                fname=view.file_name(),
                code=get_code(view),
                extra=True)
            hints += resp['hints']

        hints = [x for x in hints if x['name'].lower().startswith(self.word)]

        # Ignore if there are no hints to show
        if not hints:
            return

        hints.sort(key=lambda x: x['name'])
        #self.hints = convert_hints(_GLOBALS.get(view.id(), []))

        def format(hint):
            # Note: Sublime expects lists not tuples
            if hint['node'] == 'Namespace':
                return [
                    hint['name'].ljust(100) + u'Ⓝ ',
                    'namespace %s' % hint['full']
                ]
            elif hint['node'] == 'Type':
                if 'Class' in hint['info']:
                    sym = u'Ⓒ '
                elif 'Interface' in hint['info']:
                    sym = u'Ⓘ '
                elif 'Struct' in hint['info']:
                    sym = u'Ⓢ '
                elif 'Enum' in hint['info']:
                    sym = u'Ⓔ '
                else:
                    sym = '  '
                return [
                    hint['name'].ljust(100) + sym,
                    ' '.join([x.strip().lower() for x in sorted(hint['info'].split(','))]) + ' ' + hint['full']
                ]
            elif hint['node'] == 'Method':
                name = format_method(hint, '{name} ({params})', '{name}')
                sign = format_method(hint, '({params}): {return}', '{type}')
                return [
                    name.ljust(100, ' ') + u'Ⓜ ',
                    sign
                ]
            elif hint['node'] == 'Field':
                return [
                    hint['name'].ljust(100, ' ') + u'Ⓕ ',
                    hint['type']
                ]
            elif hint['node'] == 'Property':
                return [
                    u'Ⓟ ' + hint['name'],
                    hint['type']
                ]
            elif hint['node'] in ('Local', 'Parameter'):
                return [
                    hint['name'].ljust(100) + u'Ⓛ ',
                    hint['type']
                ]

            return [hint['name'], hint['type']]

        # Ignore constructors
        hints = [x for x in hints if x['node'] != 'Constructor']

        self.hints = hints
        hints = [format(x) for x in hints]

        flags = sublime.MONOSPACE_FONT
        selected = 0
        view.window().show_quick_panel(hints, self.on_select, flags, selected)

    def on_select(self, idx):
        if idx < 0:
            return

        hint = self.hints[idx]
        hint = hint['name']
        if len(self.word) > 0:
            hint = hint[len(self.word):]

        view = self.view
        edit = view.begin_edit()
        for s in view.sel():
            view.insert(edit, s.a, hint)
        view.end_edit(edit)


class BooShowInfoCommand(TextCommand):
    """ Shows an output panel with information about the currently
        focused entity
    """

    def run(self, edit):
        view = self.view

        ofs = view.sel()[0].a

        ch = view.substr(ofs)
        word = view.substr(view.word(ofs)).rstrip('\r\n')
        if ch == '.':
            return
        elif ch in (' ', ',', '(', ')'):
            # Silly algorithm to detect if we are in the middle of a method call
            # If there are more parens open than closed (unbalanced) remove all the
            # ones balanced.
            line = view.substr(sublime.Region(view.line(ofs).a, ofs))
            print 'L "%s"' % line
            unbalanced = 1
            idx = len(line)
            while unbalanced > 0 and idx > 0:
                idx -= 1
                if line[idx] == '(':
                    unbalanced -= 1
                elif line[idx] == ')':
                    unbalanced += 1

            if unbalanced != 0:
                view.erase_status('boo.signature')
                return

            ofs = ofs - len(line) + idx
            ofs = view.word(ofs).a

        elif word.isalnum() and word not in ('if', 'elif', 'else', 'for', 'while', 'try', 'except', 'ensure', 'def', 'class', 'struct', 'interface', 'continue', 'return', 'yield', 'true', 'false', 'null', 'in', 'of'):
            ofs = view.word(ofs).a

        # Comment out the end of line to try to make it compile without errors
        until = view.word(ofs).b
        code = get_code(view)
        #code = code[0:until] + ' # ' + code[until:]
        print 'CODE:', code[until-5:until+5]

        row, col = view.rowcol(ofs)
        resp = server(view).query(
            'entity',
            fname=view.file_name(),
            code=code,
            line=row + 1,
            column=col + 1,
            extra=True,
            params=[True]  # Request all candidate entities based on name
        )

        self.panel = view.window().get_output_panel('boo.info')

        if not len(resp['hints']):
            edit = self.panel.begin_edit()
            self.panel.replace(edit, sublime.Region(0, self.panel.size()), '')
            self.panel.end_edit(edit)
            self.panel.show(0)
            return

        edit = self.panel.begin_edit()

        hint = resp['hints'][0]
        self.panel.insert(edit, self.panel.size(), hint['full'] + ':\n\n')


        for hint in resp['hints']:
            if hint['node'] == 'Method':
                self.panel.insert(
                    edit,
                    self.panel.size(),
                    '  ' + format_method(hint, 'def ({params}): {return}', '{name}: {type}') + '\n'
                )
                if hint.get('doc'):
                    lines = hint['doc'].strip().split('\n')
                    for line in lines:
                        self.panel.insert(edit, self.panel.size(), '    # ' + line.strip() + '\n')

            else:
                self.panel.insert(edit, self.panel.size(), '  ' + hint['node'] + ': ' + hint['info'] + '\n')

        self.panel.end_edit(edit)
        self.panel.show(0)
        self.view.window().run_command('show_panel', {'panel': 'output.boo.info'})


class BooOutlineCommand(TextCommand):
    """ Generate an outline for the current file

        TODO: Generate into an scratch buffer
        TODO: Keep it synchronized with the current view?
        TODO: Colored syntax
        TODO: Navigate on double click and keyboard trigger
    """

    def run(self, edit):
        resp = server(self.view).query(
            'outline',
            fname=self.view.file_name(),
            code=get_code(self.view))

        view = self.view.window().get_output_panel('boo.outline')
        edit = view.begin_edit()
        view.insert(edit, view.size(), '\n'.join(self.render(resp)))
        view.end_edit(edit)
        view.show(view.size())
        self.view.window().run_command('show_panel', {'panel': 'output.boo.outline'})

    def render(self, node, indent=0):
        mapping = {
            'Import': 'import',
            'ClassDefinition': 'class',
            'Method': 'def',
        }

        lines = []

        lines.append('{0} {1}'.format(
            mapping.get(node['type'], node['type']),
            node.get('desc', node.get('name'))
        ))
        for member in node['members']:
            lines.extend(self.render(member, indent+1))

        if node['type'] == 'ClassDefinition':
            lines.append('')

        return [('  ' * indent) + ln for ln in lines]


class BooGotoDeclarationCommand(TextCommand):
    """ Navigate to the declaration for the selected symbol.
        If multiple choices are available a quick panel will be shown to choose
        one of them.
        When no declaration is found a message will be displayed briefly in the
        status bar.
    """

    def run(self, edit):
        # Get the position at the start of the symbol
        offset = self.view.sel()[0].a
        offset = self.view.word(offset).a
        row, col = self.view.rowcol(offset)

        resp = server(self.view).query(
            'entity',
            fname=self.view.file_name(),
            code=get_code(self.view),
            line=row + 1,
            column=col + 1,
            extra=True
        )

        self.hints = [x for x in resp['hints'] if x.get('loc')]
        if not self.hints:
            self.set_status(self.view, 'GoTo: Unable to find a definition for the selected symbol')
            return

        # Automatically show the target if there is a single one
        if len(self.hints) == 1:
            self.on_select(0)
            return

        items = []
        for hint in self.hints:
            items.append([hint['type'], hint['loc']])

        flags = 0
        selected = 0
        self.view.window().show_quick_panel(items, self.on_select, flags, selected)

    def on_select(self, idx):
        hint = self.hints[idx]
        loc = hint['loc'].split(':')
        col = int(loc.pop())
        ln = int(loc.pop())
        filepath = ':'.join(loc)

        # Dirty way to check if we will be able to open the file
        try:
            open(filepath, 'r').close()
        except:
            self.set_status(self.view, 'GoTo: Unable to open target file "{0}"'.format(filepath))
            return

        # Trigger the file open
        view = self.view.window().open_file(filepath, sublime.TRANSIENT)

        def focus():
            if view.is_loading():
                print 'View not ready yet...'
                sublime.set_timeout(focus, 50)
                return

            # Many hints may be approximations, find the actual one in surrounding lines
            # The strange sequence of negative and positive numbers is because there is a
            # tendency to report lines way above the actual symbol for fields for example.
            rex = re.compile(r'\b{0}\b'.format(re.escape(hint['name'])))
            row = ln-1
            for idx in (0, -1, 1, -2, 2, -3, 3, 4, 5, 6, 7, 8, 9):
                line = view.line(view.text_point(row + idx, 0))
                match = rex.search(view.substr(line))
                if match:
                    ofs = line.a + match.start()
                    view.show_at_center(ofs)
                    view.sel().clear()
                    view.sel().add(line)
                    return

            # If no exact location was found try to center the view
            view.show_at_center(view.text_point(row, 0))
            self.set_status(view, 'GoTo: Unable to find the exact location')

        focus()

    def set_status(self, view, msg):
        view.set_status('boo.command', msg)
        sublime.set_timeout(lambda: view.erase_status('boo.command'), 4000)


class BooFindUsagesCommand(TextCommand):
    """
    TODO: Implement Find Usages by sending the server a list of files, it will
          then visit them looking for the desired entity.
    """
    def run(self, edit):
        pass
