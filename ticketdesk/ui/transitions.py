# SPDX-License-Identifier: GPL-2.0-or-later
"""Status change flow shared by the ticket view and boards.

1. Ask for fields the transition screen requires.
2. Send the transition. If a workflow rule refuses it (e.g. "fill in Cost center"),
   find those fields on the edit screen, ask for them, save them, and retry once more.
Exactly one of on_done(issue) / on_failed(message or None) is called; None means the
user cancelled.
"""

from ..core import fields as F
from ..core.api import JiraError
from .dialogs import FieldFormDialog


class TransitionFlow:
    def __init__(self, widget, store, key, current_fields, transition, on_done, on_failed):
        self.widget = widget
        self.store = store
        self.key = key
        self.fields = current_fields or {}
        self.t = transition
        self.on_done = on_done
        self.on_failed = on_failed
        self.target = (transition.get('to') or {}).get('name') or transition.get('name')
        self._finished = False
        self._open = 0
        self._fix_dialog = None
        self._fix_offered = False
        self._busy = False

    def start(self):
        required = [(fid, m) for fid, m in (self.t.get('fields') or {}).items() if m.get('required')]
        if required:
            self._present(FieldFormDialog(f'Move to {self.target}', self.key, required, self.fields, 'Move',
                                          lambda values, d: self._run(values, d)))
        else:
            self._run(None, None)

    # ----- dialogs -----

    def _present(self, dialog):
        self._open += 1
        dialog.connect('closed', self._closed)
        dialog.present(self.widget.get_root())

    def _closed(self, _dialog):
        self._open -= 1
        if self._open == 0 and not self._finished and not self._busy:
            self._finish(None, None)  # cancelled; a request in flight finishes the flow itself

    def _finish(self, issue, message):
        if self._finished:
            return
        self._finished = True
        if issue is not None:
            self.on_done(issue)
        else:
            self.on_failed(message)

    # ----- requests -----

    def _run(self, values, dialog):
        self._busy = True

        def done(issue):
            self._busy = False
            self._finish(issue, None)
            if dialog:
                dialog.close()

        def failed(e):
            self._busy = False
            if isinstance(e, JiraError) and e.status == 400 and not self._fix_offered:
                self._offer_rule_fix(values, e, dialog)
            elif dialog and self._open:
                dialog.show_error(str(e))
            else:
                self._finish(None, f'Could not move {self.key}: {e}')
        self.store.do_transition(self.key, self.t['id'], values, done, failed)

    def _offer_rule_fix(self, values, error, dialog):
        self._fix_offered = True

        def refused():
            if dialog and self._open:
                dialog.show_error(str(error))
            else:
                self._finish(None, f'Jira refused the move: {error}')

        def got_meta(meta):
            fix = [(fid, {**m, 'required': True}) for fid, m in F.fields_for_rule_error(meta, error)]
            if not fix:
                refused()
                return
            reason = '; '.join(error.messages + list(error.field_errors.values())) or str(error)

            def submit(fix_values, d):
                def not_saved(e):
                    self._busy = False
                    if self._open:
                        d.show_error(f'Could not save: {e}')
                    else:
                        self._finish(None, f'Could not save {self.key}: {e}')
                self._busy = True
                self.store.edit_fields(self.key, fix_values, lambda _issue: self._run(values, d), not_saved)
            self._fix_dialog = FieldFormDialog(f'Move to {self.target}', self.key, fix, self.fields,
                                               'Save and Move', submit, note=f'Jira refused the move: {reason}')
            self._present(self._fix_dialog)  # open the new dialog before closing the old one
            if dialog:
                dialog.close()
        self._busy = True

        def meta_done(meta):
            self._busy = False
            if dialog and not self._open:  # the user closed the form meanwhile
                self._finish(None, f'Jira refused the move: {error}')
            else:
                got_meta(meta)

        def meta_failed(_e):
            self._busy = False
            refused()
        self.store.edit_meta(self.key, meta_done, meta_failed)
