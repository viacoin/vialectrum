from kivy.app import App
from kivy.factory import Factory
from kivy.properties import ObjectProperty
from kivy.lang import Builder
from decimal import Decimal

Builder.load_string('''
<AddressLabel@Label>
    text_size: self.width, None
    halign: 'left'
    valign: 'top'

<AddressItem@CardItem>
    address: ''
    memo: ''
    amount: ''
    status: ''
    BoxLayout:
        spacing: '8dp'
        height: '32dp'
        orientation: 'vertical'
        Widget
        AddressLabel:
            text: root.address
            shorten: True
        Widget
        AddressLabel:
            text: (root.amount if root.status == 'Funded' else root.status) + '     ' + root.memo
            color: .699, .699, .699, 1
            font_size: '13sp'
            shorten: True
        Widget

<AddressesDialog@Popup>
    id: popup
    title: _('Addresses')
    message: ''
    pr_status: 'Pending'
    show_change: 0
    show_used: 0
    on_message:
        self.update()
    BoxLayout:
        id:box
        padding: '12dp', '70dp', '12dp', '12dp'
        spacing: '12dp'
        orientation: 'vertical'
        size_hint: 1, 1.1
        BoxLayout:
            spacing: '6dp'
            size_hint: 1, None
            orientation: 'horizontal'
            AddressFilter:
                opacity: 1
                size_hint: 1, None
                height: self.minimum_height
                spacing: '5dp'
                AddressButton:
                    id: search
                    text: {0:_('Receiving'), 1:_('Change'), 2:_('All')}[root.show_change]
                    on_release:
                        root.show_change = (root.show_change + 1) % 3
                        Clock.schedule_once(lambda dt: root.update())
            AddressFilter:
                opacity: 1
                size_hint: 1, None
                height: self.minimum_height
                spacing: '5dp'
                AddressButton:
                    id: search
                    text: {0:_('All'), 1:_('Unused'), 2:_('Funded'), 3:_('Used')}[root.show_used]
                    on_release:
                        root.show_used = (root.show_used + 1) % 4
                        Clock.schedule_once(lambda dt: root.update())
            AddressFilter:
                opacity: 1
                size_hint: 1, None
                height: self.minimum_height
                spacing: '5dp'
                canvas.before:
                    Color:
                        rgba: 0.9, 0.9, 0.9, 1
                AddressButton:
                    id: change
                    text: root.message if root.message else _('Search')
                    on_release: Clock.schedule_once(lambda dt: app.description_dialog(popup))
        ScrollView:
            GridLayout:
                cols: 1
                id: search_container
                size_hint_y: None
                height: self.minimum_height
''')


from electrum_ltc_gui.kivy.i18n import _
from electrum_ltc_gui.kivy.uix.context_menu import ContextMenu


class EmptyLabel(Factory.Label):
    pass


class AddressesDialog(Factory.Popup):

    def __init__(self, app, screen, callback):
        Factory.Popup.__init__(self)
        self.app = app
        self.screen = screen
        self.callback = callback
        self.cards = {}
        self.context_menu = None

    def get_card(self, addr, balance, is_used, label):
        ci = self.cards.get(addr)
        if ci is None:
            ci = Factory.AddressItem()
            ci.screen = self
            ci.address = addr
            self.cards[addr] = ci

        ci.memo = label
        ci.amount = self.app.format_amount_and_units(balance)
        request = self.app.wallet.get_payment_request(addr, self.app.electrum_config)
        if is_used:
            ci.status = _('Used')
        else:
            ci.status = _('Funded') if balance > 0 else _('Unused')
        return ci


    def update(self):
        self.menu_actions = [(_('Use'), self.do_show), (_('Details'), self.do_view)]
        wallet = self.app.wallet
        if self.show_change == 0:
            _list = wallet.get_receiving_addresses()
        elif self.show_change == 1:
            _list = wallet.get_change_addresses()
        else:
            _list = wallet.get_addresses()
        search = self.message
        container = self.ids.search_container
        container.clear_widgets()
        n = 0
        for address in _list:
            label = wallet.labels.get(address, '')
            balance = sum(wallet.get_addr_balance(address))
            is_used = wallet.is_used(address)
            if self.show_used == 1 and (balance or is_used):
                continue
            if self.show_used == 2 and balance == 0:
                continue
            if self.show_used == 3 and not is_used:
                continue
            card = self.get_card(address, balance, is_used, label)
            if search and not self.ext_search(card, search):
                continue
            container.add_widget(card)
            n += 1
        if not n:
            msg = _('No address matching your search')
            container.add_widget(EmptyLabel(text=msg))

    def do_show(self, obj):
        self.hide_menu()
        self.dismiss()
        self.app.show_request(obj.address)

    def do_view(self, obj):
        req = self.app.wallet.get_payment_request(obj.address, self.app.electrum_config)
        if req:
            c, u, x = self.app.wallet.get_addr_balance(obj.address)
            balance = c + u + x
            if balance > 0:
                req['fund'] = balance
            status = req.get('status')
            amount = req.get('amount')
            address = req['address']
            if amount:
                status = req.get('status')
                status = request_text[status]
            else:
                received_amount = self.app.wallet.get_addr_received(address)
                status = self.app.format_amount_and_units(received_amount)
            self.app.show_pr_details(req, status, False)

        else:
            req = { 'address': obj.address, 'status' : obj.status }
            status = obj.status
            c, u, x = self.app.wallet.get_addr_balance(obj.address)
            balance = c + u + x
            if balance > 0:
                req['fund'] = balance
            self.app.show_addr_details(req, status)

    def do_delete(self, obj):
        from .dialogs.question import Question
        def cb(result):
            if result:
                self.app.wallet.remove_payment_request(obj.address, self.app.electrum_config)
                self.update()
        d = Question(_('Delete request?'), cb)
        d.open()

    def ext_search(self, card, search):
        return card.memo.find(search) >= 0 or card.amount.find(search) >= 0
        
    def show_menu(self, obj):
        self.hide_menu()
        self.context_menu = ContextMenu(obj, self.menu_actions)
        self.ids.box.add_widget(self.context_menu)

    def hide_menu(self):
        if self.context_menu is not None:
            self.ids.box.remove_widget(self.context_menu)
            self.context_menu = None
