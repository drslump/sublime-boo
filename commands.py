import re
import sublime
from sublime_plugin import TextCommand, WindowCommand


MEMBER_REGEX = re.compile(r'[\w\)\]]\.$')


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
            sublime.set_timeout(self.delayed_complete, 1)

    def delayed_complete(self):
        self.view.run_command("auto_complete")


class BooQuickPanelCompleteCommand(WindowCommand):

    def run(self):
        # TODO: Pre filter the results based on the already written characters

        self.hints = [
            ['Entity', 'Boo.Ide.CompletionProposal.Entity'],
            ['Name', 'Boo.Ide.CompletionProposal.Name'],
            ['EntityType', 'Boo.Ide.CompletionProposal.EntityType'],
            ['Description', 'Boo.Ide.CompletionProposal.Description'],
            ['Equals(obja as object, count as int, messages as (string), errors as List[of string], extra as Hash, node as Boo.Lang.Compiler.Ast.Reference) as bool',
             '(obja: object, count: int, messages: (string), errors: List[of string], extra: Hash, node: Boo.Lang.Compiler.Ast.Reference) as bool'],
            ['GetHashCode(): int',
             '(foo: string, bar: object, node: Boo.Lang.Compiler.Ast.MethodInvocationExpression): int'],
            ['GetType(foo, bar, baz, node): System.Type',
             '(foo: string, bar: object, baz: double, node: Boo.Lang.Compiler.Ast.MethodInvocationExpression): System.Type'],
            ['ToString()', 'System.String ToString()']
        ]

        # Ignore if there are no hints to show
        if not self.hints:
            return

        flags = 0  # sublime.MONOSPACE_FONT
        selected = 0
        self.window.show_quick_panel(self.hints, self.on_select, flags, selected)

    def on_select(self, idx):
        if idx < 0:
            return

        hint = self.hints[idx]

        view = self.window.active_view()
        edit = view.begin_edit()
        for s in view.sel():
            view.insert(edit, s.a, hint[0])
        view.end_edit(edit)


class BooGotoDeclarationCommand(TextCommand):

    def run(self, edit):
        # Get the position at the start of the symbol
        caret = self.view.sel()[0].begin()
        caret = self.view.word(caret).a
        row, col = self.view.rowcol(caret)

        print 'TODO BooGoToDeclaration: %d:%d' % (row + 1, col + 1)


class BooFindUsagesCommand(TextCommand):
    """
    TODO: Implement Find Usages by sending the server a list of files, it will
          then visit them looking for the desired entity.
    """
    def run(self, edit):
        pass
