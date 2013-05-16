# -*- coding: utf-8 -*-
import re
import sublime
from sublime_plugin import TextCommand, WindowCommand

from .SublimeBoo import server, get_code, convert_hint
from .BooHints import format_type, format_method, find_open_paren


MEMBER_REGEX = re.compile(r'[\w\)\]]\.$')
WORD_REGEX = re.compile(r'^\w+$')


class BooDotCompleteCommand(TextCommand):
    """ Command triggered when the dot key is pressed to show the autocomplete popup
    """
    def run(self, edit):
        print('BooDot')
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


class BooImportCommand(WindowCommand):
    """ Include an additional import for a symbol into the file 
        TODO:
            - Use a quick panel to browse namespaces
            - List already imported symbols too, if selected they are removed
            - If importing show an input panel to define an alias, prefiled 
              for types, empty or "*" for namespaces (import all)
    """

    def run(self):
        self.input = None
        self.input = self.window.show_input_panel(
            'Import',
            '',
            self.on_done,
            self.on_change,
            self.on_cancel)

    def on_done(self, text):
        print('on_done', text)

    def on_change(self, text):
        print('on_change', text)
        # First call may come before we register the view object
        if not self.input:
            return

    def on_cancel(self):
        pass


class BooQuickPanelCompleteCommand(TextCommand):

    def run(self, edit):
        self.edit = edit
        view = self.view

        # Find a preceding non-word character in the line
        offset = view.sel()[0].a

        word = view.word(offset)
        self.prefix = view.substr(word)

        # After a dot just autocomplete members
        if word.a == offset-1 and self.prefix[0] == '.':
            self.prefix = ''
        # At the end of an ident autocomplete it
        elif word.b == offset and self.prefix.isalnum():
            offset = word.a
        # Check if we are inside a call expression to report information about
        # it and its overloads
        else:
            idx = find_open_paren(view.substr(sublime.Region(0, offset)), open='([', close=')]')
            if idx is None:
                return

            word = view.word(idx)
            offset = word.a
            self.prefix = view.substr(word)

        # Request member hints
        resp = server(view).query(
            'members',
            fname=view.file_name(),
            code=get_code(view),
            offset=offset,
            extra=True)
        hints = resp['hints']

        # For not member references ask for globals
        if view.substr(offset-1) != '.':
            resp = server(view).query(
                'globals',
                fname=view.file_name(),
                code=get_code(view),
                extra=True)
            hints += resp['hints']

        # TODO: Why doesn't it work with locals?

        # Filter out entities to those we are interested in
        if len(self.prefix):
            # If followed by a paren do an exact match
            if view.substr(word.b) == '(':
                rex = re.compile(re.escape(self.prefix) + '$')
            else:
                # Emulate sublime's fuzzy search with a regexp
                #rex = re.compile(''.join('.*' + re.escape(ch) for ch in self.prefix), re.IGNORECASE)
                # TODO: Filtering by actual prefix seems to feel better. Perhaps because the
                #       ordering is very different from Sublime's weighted one.
                rex = re.compile(re.escape(self.prefix), re.IGNORECASE)

            hints = [x for x in hints if rex.match(x['name'])]

        # Ignore if there are no hints to show
        if not hints:
            return

        hints.sort(key=lambda x: x['name'])

        def format(hint):
            # Reuse standard completion formatting for the title
            # TODO: Refactor this to make it not a hack
            desc, name = convert_hint(hint)

            # Note: Sublime expects lists not tuples
            if hint['node'] == 'Namespace':
                return [
                    desc,
                    'namespace %s' % hint['full']
                ]
            elif hint['node'] == 'Type':
                return [
                    desc,
                    ' '.join([x.strip().lower() for x in sorted(hint['info'].split(','))]) + ' ' + hint['full']
                ]
            elif hint['node'] == 'Method':
                name = format_method(hint, '{name} ({params})', '{name}')
                sign = format_method(hint, 'def ({params}): {return}', '{type}')
                return [
                    u'ƒ ' + name,
                    sign
                ]

            return [desc, '{0}: {1}'.format(hint['node'].lower(), hint['type'])]

        # Ignore constructors
        hints = [x for x in hints if x['node'] != 'Constructor']

        self.hints = hints
        hints = [format(x) for x in hints]

        #hints.insert(0, [u'↵ Go to parent namespace'])
        #hints.insert(0, [u'⟳ System.Collections'])

        flags = 0  # sublime.MONOSPACE_FONT
        selected = 0
        view.window().show_quick_panel(hints, self.on_select, flags, selected)

    def on_select(self, idx):
        if idx < 0:
            return

        hint = self.hints[idx]
        hint = hint['name']
        hint = hint[len(self.prefix):]

        view = self.view
        for s in view.sel():
            view.insert(self.edit, s.a, hint)


class BooBrowseNamespacesCommand(TextCommand):
    """ Browse global namespaces
    """
    def run(self, edit):
        view = self.view

        self.list = []
        items = []

        resp = server(view).query(
            'namespaces',
            fname=view.file_name(),
            code='',
        )
        from .SublimeBoo import symbol_for
        seen = set()
        for hint in resp['hints']:
            if hint['full'] not in seen and hint['node'] == 'Namespace':
                seen.add(hint['full'])
                self.list.append(hint['full'])
                items.append([
                    u'{0} {1}'.format(symbol_for(hint), hint['full']),
                ])

        view.window().show_quick_panel(items, self.on_select)

    def on_select(self, idx):
        if idx >= 0:
            sublime.set_timeout(lambda: self.browse(self.list[idx]), 1)

    def browse(self, fullname):
        self.list = [
            '.'.join(fullname.split('.')[:-1]),
            fullname,
        ]
        items = [
            [u'⇧ Go to parent namespace'],
            [u'⟳ ' + fullname],
        ]

        resp = server(self.view).query(
            'members',
            fname=self.view.file_name(),
            code='{0}.'.format(fullname),
            offset=len(fullname)+1,
            extra=True
        )

        from .SublimeBoo import symbol_for
        for hint in resp['hints']:
            self.list.append(hint['full'])
            items.append([
                '{0} {1}'.format(symbol_for(hint), hint['name'])
            ])


        self.view.window().show_quick_panel(items, self.on_select)


class BooNavigateCommand(TextCommand):
    """ Navigates symbols, errors and namespaces
    """

    def run(self, edit):
        view = self.view

        self.actions = []
        items = []

        items.append([u'⇤ Go to previous position'])#, view.file_name() + ':89'])
        self.actions.append(None)
        items.append([u'⇥ Go to next position'])#, view.file_name() + ':180'])
        self.actions.append(None)
        items.append([u'📁 Browse namespaces'])#, ''])
        self.actions.append((self.command, 'boo_browse_namespaces'))
        #items.append([u'⇧ Go to parent namespace', 'System'])
        #items.append([u'⟳ System.Diagnostics', 'Select to reload'])


        from .SublimeBoo import _LINTS
        lints = _LINTS.get(view.id(), {})
        for line, lint in lints.items():
            # TODO: Handle column
            if 'BCE' in lint:
                lint = u'✖ ' + lint
            else:
                lint = u'⚠ ' + lint
            items.append([lint, '{0}:{1}'.format(view.file_name(), line)])
            self.actions.append((self.goto, view.file_name(), line + 1))

        from .SublimeBoo import _GLOBALS, symbol_for
        hints = _GLOBALS.get(view.id(), [])
        items += [symbol_for(x) + ' ' + x['name'] for x in hints if x['node'] == 'Namespace']

        view.window().show_quick_panel(items, self.on_select)

    def on_select(self, idx):
        print('Navigate idx', idx)
        action = self.actions[idx]
        action[0](*action[1:])

    def goto(self, fname, line = 0, column = 0):
        self.view.window().open_file(
            '{0}:{1}:{2}'.format(fname, line, column),
            sublime.TRANSIENT | sublime.ENCODED_POSITION)

    def command(self, command):
        sublime.set_timeout(lambda: self.view.window().run_command(command, {}), 1)


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
            # TODO: use find_open_paren
            # Silly algorithm to detect if we are in the middle of a method call
            # If there are more parens open than closed (unbalanced) remove all the
            # ones balanced.
            line = view.substr(sublime.Region(view.line(ofs).a, ofs))
            print('L "%s"' % line)
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
        print('CODE:', code[until-5:until+5])

        row, col = view.rowcol(ofs)
        resp = server(view).query(
            'entity',
            fname=view.file_name(),
            code=code,
            line=row + 1,
            column=col + 1,
            extra=True,
            params=(True,)  # Request all candidate entities based on name
        )

        self.panel = view.window().get_output_panel('boo.info')

        if not len(resp['hints']):
            self.panel.replace(edit, sublime.Region(0, self.panel.size()), '')
            self.panel.show(0)
            return

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
        view.insert(edit, view.size(), '\n'.join(self.render(resp)))
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


class BooGoToImports(TextCommand):
    """ Jumps to the imports section of the current file
    """

    def run(self, edit):
        resp = server(self.view).query(
            command='outline',
            fname=self.view.file_name(),
            code=get_code(self.view)
        )

        imports = [x for x in resp['members'] if x['type'] == 'Import']
        if len(imports):
            imports.sort(key=lambda x: x['line'])
            ln = imports[-1]['line']
        else:
            ln = 1

        target = self.view.text_point(ln, 0)
        self.view.show_at_center(target)
        self.view.sel().clear()
        self.view.sel().add(target)


class BooGoToError(TextCommand):
    """ Jumps to next error
    """

    def run(self, edit, reverse=False):
        ofs = self.view.sel()[-1].a
        ln = self.view.rowcol(ofs)[0]

        # TODO: Take column into consideration

        # Get lines with lints
        from SublimeBoo import _LINTS
        lines = _LINTS.get(self.view.id(), {}).keys()
        # Make sure its sorted
        lines.sort(reverse=reverse)

        for line in lines:
            if (not reverse and line > ln) or (reverse and line < ln):
                target = self.view.text_point(line, 1)
                self.view.show_at_center(target)
                self.view.sel().clear()
                self.view.sel().add(target)
                return


class BooGoToEnclosingType(TextCommand):
    """ Jumps to the enclosing type for the current position
    """

    def run(self, edit):
        resp = server(self.view).query(
            command='outline',
            fname=self.view.file_name(),
            code=get_code(self.view)
        )

        ofs = self.view.sel()[-1].a
        ln = self.view.rowcol(ofs)[0]

        def extract_types(root):
            accepted = ('ClassDefinition', 'InterfaceDefinition', 'StructDefinition', 'EnumDefinition')
            types = []
            for node in root['members']:
                if node['type'] in accepted:
                    types += extract_types(node)
                    types.append(node)
            return types

        # Extract all type definitions from the outline
        types = extract_types(resp)
        # Sort them from bottom to top
        types = sorted(types, key=lambda x: x['line'], reverse=True)

        for node in types:
            if ln > node['line'] and ln <= node['line'] + node['length']:
                target = self.view.text_point(node['line'], 1)
                self.view.show_at_center(target)
                self.view.sel().clear()
                self.view.sel().add(target)
                return


class BooGoToMain(TextCommand):
    """ Jumps to the main section of the current project/directory/file
    """

    def run(self, edit):
        # TODO
        pass


class BooGoToDeclarationCommand(TextCommand):
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
                print('View not ready yet...')
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
                    #view.sel().add(view.word(ofs))
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
