import re
import sublime
from sublime_plugin import TextCommand, WindowCommand

from SublimeBoo import query_members, server, get_code


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


class BooQuickPanelCompleteCommand(WindowCommand):

    def run(self):
        view = self.window.active_view()

        # Find a preceding non-word character in the line
        offset = view.sel()[0].a
        self.word = view.substr(view.word(offset))
        if self.word != '.':
            offset -= len(self.word)

        # TODO: Obtain proper hints like we do on dot
        # TODO: Work with entities so we can offer good signatures

        hints = query_members(view, offset)

        # If it's not a prefix just ignore it
        if not WORD_REGEX.search(self.word):
            self.word = ''
        else:
            hints = [x for x in hints if x[0].startswith(self.word)]

        #self.hints = convert_hints(_GLOBALS.get(view.id(), []))

        # Ignore if there are no hints to show
        if not hints:
            return

        # HACK: Sublime expects lists and not tuples
        self.hints = [list(x) for x in hints]

        flags = 0  # sublime.MONOSPACE_FONT
        selected = 0
        view.window().show_quick_panel(self.hints, self.on_select, flags, selected)

    def on_select(self, idx):
        if idx < 0:
            return

        hint = self.hints[idx]
        hint = hint[0].split('\t')[0]
        if len(self.word) > 0:
            hint = hint[len(self.word):]

        view = self.window.active_view()
        edit = view.begin_edit()
        for s in view.sel():
            view.insert(edit, s.a, hint)
        view.end_edit(edit)


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

        print resp
        print '\n'.join(self.render(resp))

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
            column=col + 1
        )

        self.hints = resp['hints']
        if not self.hints:
            self.view.set_status('Boo-Command', 'GoTo: Unable to find a definition for the selected symbol')
            sublime.set_timeout(lambda: self.view.erase_status('Boo-Command'), 3000)
            return

        print self.hints

        # Automatically show the target if there is a single one
        if len(self.hints) == 1:
            self.on_select(0)
            return

        items = []
        for hint in self.hints:
            items.append([hint['type'], hint['file']])

        flags = 0  # sublime.MONOSPACE_FONT
        selected = 0
        self.view.window().show_quick_panel(items, self.on_select, flags, selected)

    def on_select(self, idx):
        hint = self.hints[idx]
        self.view.window().open_file(
            '{0}:{1}:{2}'.format(hint['file'], hint['line']-1, hint['column']-1),
            sublime.ENCODED_POSITION | sublime.TRANSIENT)


class BooFindUsagesCommand(TextCommand):
    """
    TODO: Implement Find Usages by sending the server a list of files, it will
          then visit them looking for the desired entity.
    """
    def run(self, edit):
        pass
