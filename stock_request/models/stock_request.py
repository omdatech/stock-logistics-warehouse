# Copyright 2017 Eficent Business and IT Consulting Services, S.L.
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons import decimal_precision as dp
from odoo.tools import float_compare


REQUEST_STATES = [
    ('draft', 'Draft'),
    ('open', 'In progress'),
    ('done', 'Done'),
    ('cancel', 'Cancelled')]


class StockRequest(models.Model):
    _name = "stock.request"
    _description = "Stock Request"
    _inherit = ['mail.thread', 'mail.activity.mixin']

    @api.model
    def default_get(self, fields):
        res = super(StockRequest, self).default_get(fields)
        warehouse = None
        if 'warehouse_id' not in res and res.get('company_id'):
            warehouse = self.env['stock.warehouse'].search(
                [('company_id', '=', res['company_id'])], limit=1)
        if warehouse:
            res['warehouse_id'] = warehouse.id
            res['location_id'] = warehouse.lot_stock_id.id
        return res

    def _get_default_requested_by(self):
        return self.env['res.users'].browse(self.env.uid)

    @api.depends('product_id', 'product_uom_id', 'product_uom_qty')
    def _compute_product_qty(self):
        for rec in self:
            rec.product_qty = rec.product_uom_id._compute_quantity(
                rec.product_uom_qty, rec.product_id.uom_id)

    name = fields.Char(
        'Name', copy=False, required=True, readonly=True,
        states={'draft': [('readonly', False)]},
        default='/')
    state = fields.Selection(selection=REQUEST_STATES, string='Status',
                             copy=False, default='draft', index=True,
                             readonly=True, track_visibility='onchange',
                             )
    requested_by = fields.Many2one(
        'res.users', 'Requested by', required=True,
        track_visibility='onchange',
        default=lambda s: s._get_default_requested_by(),
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse', 'Warehouse', readonly=True,
        ondelete="cascade", required=True,
        states={'draft': [('readonly', False)]})
    location_id = fields.Many2one(
        'stock.location', 'Location', readonly=True,
        domain=[('usage', 'in', ['internal', 'transit'])],
        ondelete="cascade", required=True,
        states={'draft': [('readonly', False)]},
    )
    product_id = fields.Many2one(
        'product.product', 'Product', readonly=True,
        states={'draft': [('readonly', False)]},
        domain=[('type', 'in', ['product', 'consu'])], ondelete='cascade',
        required=True,
    )
    product_uom_id = fields.Many2one(
        'product.uom', 'Product Unit of Measure',
        readonly=True, required=True,
        states={'draft': [('readonly', False)]},
        default=lambda self: self._context.get('product_uom_id', False),
    )
    product_uom_qty = fields.Float(
        'Quantity', digits=dp.get_precision('Product Unit of Measure'),
        states={'draft': [('readonly', False)]},
        readonly=True, required=True,
        help="Quantity, specified in the unit of measure indicated in the "
             "request.",
    )
    product_qty = fields.Float(
        'Real Quantity', compute='_compute_product_qty',
        store=True, readonly=True, copy=False,
        help='Quantity in the default UoM of the product',
    )
    procurement_group_id = fields.Many2one(
        'procurement.group', 'Procurement Group', readonly=True,
        states={'draft': [('readonly', False)]},
        help="Moves created through this stock request will be put in this "
             "procurement group. If none is given, the moves generated by "
             "procurement rules will be grouped into one big picking.",
    )
    company_id = fields.Many2one(
        'res.company', 'Company', required=True, readonly=True,
        states={'draft': [('readonly', False)]},
        default=lambda self: self.env['res.company']._company_default_get(
            'stock.request'),
    )
    expected_date = fields.Datetime(
        'Expected Date', default=fields.Datetime.now, index=True,
        required=True, readonly=True,
        states={'draft': [('readonly', False)]},
        help="Date when you expect to receive the goods.",
    )
    picking_policy = fields.Selection([
        ('direct', 'Receive each product when available'),
        ('one', 'Receive all products at once')],
        string='Shipping Policy', required=True, readonly=True,
        states={'draft': [('readonly', False)]},
        default='direct',
    )
    move_ids = fields.One2many(comodel_name='stock.move',
                               compute='_compute_move_ids',
                               string='Stock Moves', readonly=True,
                               )
    picking_ids = fields.One2many('stock.picking',
                                  compute='_compute_picking_ids',
                                  string='Pickings', readonly=True,
                                  )
    qty_in_progress = fields.Float(
        'Qty In Progress', digits=dp.get_precision('Product Unit of Measure'),
        readonly=True, compute='_compute_qty', store=True,
        help="Quantity in progress.",
    )
    qty_done = fields.Float(
        'Qty Done', digits=dp.get_precision('Product Unit of Measure'),
        readonly=True, compute='_compute_qty', store=True,
        help="Quantity completed",
    )
    picking_count = fields.Integer(string='Delivery Orders',
                                   compute='_compute_picking_ids',
                                   readonly=True,
                                   )
    route_id = fields.Many2one('stock.location.route', string='Route',
                               readonly=True,
                               states={'draft': [('readonly', False)]},
                               domain="[('id', 'in', route_ids)]",
                               ondelete='restrict')

    route_ids = fields.Many2many(
        'stock.location.route', string='Route',
        compute='_compute_route_ids',
        readonly=True,
    )

    allocation_ids = fields.One2many(comodel_name='stock.request.allocation',
                                     inverse_name='stock_request_id',
                                     string='Stock Request Allocation')

    order_id = fields.Many2one(
        'stock.request.order',
        readonly=True,
    )

    _sql_constraints = [
        ('name_uniq', 'unique(name, company_id)',
         'Stock Request name must be unique'),
    ]

    @api.depends('allocation_ids')
    def _compute_move_ids(self):
        for request in self.sudo():
            request.move_ids = request.allocation_ids.mapped('stock_move_id')

    @api.depends('allocation_ids')
    def _compute_picking_ids(self):
        for request in self.sudo():
            request.picking_count = 0
            request.picking_ids = self.env['stock.picking']
            request.picking_ids = request.move_ids.filtered(
                lambda m: m.state != 'cancel').mapped('picking_id')
            request.picking_count = len(request.picking_ids)

    @api.depends('allocation_ids', 'allocation_ids.stock_move_id.state',
                 'allocation_ids.stock_move_id.move_line_ids',
                 'allocation_ids.stock_move_id.move_line_ids.qty_done')
    def _compute_qty(self):
        for request in self.sudo():
            done_qty = sum(request.allocation_ids.mapped(
                'allocated_product_qty'))
            open_qty = sum(request.allocation_ids.mapped('open_product_qty'))
            request.qty_done = request.product_id.uom_id._compute_quantity(
                done_qty, request.product_uom_id)
            request.qty_in_progress = \
                request.product_id.uom_id._compute_quantity(
                    open_qty, request.product_uom_id)

    @api.depends('product_id', 'warehouse_id', 'location_id')
    def _compute_route_ids(self):
        for record in self:
            routes = self.env['stock.location.route']
            if record.product_id:
                routes += record.product_id.mapped(
                    'route_ids') | record.product_id.mapped(
                    'categ_id').mapped('total_route_ids')
            if record.warehouse_id:
                routes |= self.env['stock.location.route'].search(
                    [('warehouse_ids', 'in', record.warehouse_id.ids)])
            parents = record.get_parents().ids
            record.route_ids = routes.filtered(lambda r: any(
                p.location_id.id in parents for p in r.pull_ids))

    def get_parents(self):
        location = self.location_id.sudo()
        result = location
        while location.location_id:
            location = location.location_id
            result |= location
        return result

    @api.constrains('company_id', 'product_id', 'warehouse_id',
                    'location_id', 'route_id')
    def _check_company_constrains(self):
        """ Check if the related models have the same company """
        for rec in self:
            if rec.product_id.company_id and \
                    rec.product_id.company_id != rec.company_id:
                raise ValidationError(
                    _('You have entered a product that is assigned '
                      'to another company.'))
            if rec.location_id.company_id and \
                    rec.location_id.company_id != rec.company_id:
                raise ValidationError(
                    _('You have entered a location that is '
                      'assigned to another company.'))
            if rec.warehouse_id.company_id != rec.company_id:
                raise ValidationError(
                    _('You have entered a warehouse that is '
                      'assigned to another company.'))
            if rec.route_id and rec.route_id.company_id and \
                    rec.route_id.company_id != rec.company_id:
                raise ValidationError(
                    _('You have entered a route that is '
                      'assigned to another company.'))

    @api.constrains('product_id')
    def _check_product_uom(self):
        ''' Check if the UoM has the same category as the
        product standard UoM '''
        if any(request.product_id.uom_id.category_id !=
                request.product_uom_id.category_id for request in self):
            raise ValidationError(
                _('You have to select a product unit of measure in the '
                  'same category than the default unit '
                  'of measure of the product'))

    @api.constrains('order_id', 'requested_by')
    def check_order_requested_by(self):
        if self.order_id and self.order_id.requested_by != self.requested_by:
            raise ValidationError(_(
                'Requested by must be equal to the order'
            ))

    @api.constrains('order_id', 'warehouse_id')
    def check_order_warehouse_id(self):
        if self.order_id and self.order_id.warehouse_id != self.warehouse_id:
            raise ValidationError(_(
                'Warehouse must be equal to the order'
            ))

    @api.constrains('order_id', 'location_id')
    def check_order_location(self):
        if self.order_id and self.order_id.location_id != self.location_id:
            raise ValidationError(_(
                'Location must be equal to the order'
            ))

    @api.constrains('order_id', 'procurement_group_id')
    def check_order_procurement_group(self):
        if (
            self.order_id and
            self.order_id.procurement_group_id != self.procurement_group_id
        ):
            raise ValidationError(_(
                'Procurement group must be equal to the order'
            ))

    @api.constrains('order_id', 'company_id')
    def check_order_company(self):
        if self.order_id and self.order_id.company_id != self.company_id:
            raise ValidationError(_(
                'Company must be equal to the order'
            ))

    @api.constrains('order_id', 'expected_date')
    def check_order_expected_date(self):
        if self.order_id and self.order_id.expected_date != self.expected_date:
            raise ValidationError(_(
                'Expected date must be equal to the order'
            ))

    @api.constrains('order_id', 'picking_policy')
    def check_order_picking_policy(self):
        if (
            self.order_id and
            self.order_id.picking_policy != self.picking_policy
        ):
            raise ValidationError(_(
                'The picking policy must be equal to the order'
            ))

    @api.onchange('warehouse_id')
    def onchange_warehouse_id(self):
        """ Finds location id for changed warehouse. """
        res = {'domain': {}}
        if self.warehouse_id:
            # search with sudo because the user may not have permissions
            loc_wh = self.location_id.sudo().get_warehouse()
            if self.warehouse_id != loc_wh:
                self.location_id = self.warehouse_id.lot_stock_id.id
            if self.warehouse_id.company_id != self.company_id:
                self.company_id = self.warehouse_id.company_id
        return res

    @api.onchange('location_id')
    def onchange_location_id(self):
        res = {'domain': {}}
        if self.location_id:
            loc_wh = self.location_id.get_warehouse()
            if self.warehouse_id != loc_wh:
                self.warehouse_id = loc_wh
        return res

    @api.onchange('company_id')
    def onchange_company_id(self):
        """ Sets a default warehouse when the company is changed and limits
        the user selection of warehouses. """
        if self.company_id and (
                not self.warehouse_id or
                self.warehouse_id.company_id != self.company_id):
            self.warehouse_id = self.env['stock.warehouse'].search(
                [('company_id', '=', self.company_id.id)], limit=1)
            self.onchange_warehouse_id()

        return {
            'domain': {
                'warehouse_id': [('company_id', '=', self.company_id.id)]}}

    @api.onchange('product_id')
    def onchange_product_id(self):
        res = {'domain': {}}
        if self.product_id:
            self.product_uom_id = self.product_id.uom_id.id
            res['domain']['product_uom_id'] = [
                ('category_id', '=', self.product_id.uom_id.category_id.id)]
            return res
        res['domain']['product_uom_id'] = []
        return res

    @api.multi
    def _action_confirm(self):
        self._action_launch_procurement_rule()
        self.state = 'open'

    @api.multi
    def action_confirm(self):
        self._action_confirm()
        return True

    def action_draft(self):
        self.state = 'draft'
        return True

    def action_cancel(self):
        self.sudo().mapped('move_ids')._action_cancel()
        self.state = 'cancel'
        return True

    def action_done(self):
        self.state = 'done'
        if self.order_id:
            self.order_id.check_done()
        return True

    def check_done(self):
        precision = self.env['decimal.precision'].precision_get(
            'Product Unit of Measure')
        for request in self:
            allocated_qty = sum(request.allocation_ids.mapped(
                'allocated_product_qty'))
            qty_done = request.product_id.uom_id._compute_quantity(
                allocated_qty, request.product_uom_id)
            if float_compare(qty_done, request.product_uom_qty,
                             precision_digits=precision) >= 0:
                request.action_done()
        return True

    def _prepare_procurement_values(self, group_id=False):

        """ Prepare specific key for moves or other components that
        will be created from a procurement rule
        coming from a stock request. This method could be override
        in order to add other custom key that could be used in
        move/po creation.
        """
        return {
            'date_planned': self.expected_date,
            'warehouse_id': self.warehouse_id,
            'stock_request_allocation_ids': self.id,
            'group_id': group_id or self.procurement_group_id.id or False,
            'route_ids': self.route_id,
            'stock_request_id': self.id,
        }

    @api.multi
    def _action_launch_procurement_rule(self):
        """
        Launch procurement group run method with required/custom
        fields genrated by a
        stock request. procurement group will launch '_run_move',
        '_run_buy' or '_run_manufacture'
        depending on the stock request product rule.
        """
        precision = self.env['decimal.precision'].precision_get(
            'Product Unit of Measure')
        errors = []
        for request in self:
            if (
                request.state != 'draft' or
                request.product_id.type not in ('consu', 'product')
            ):
                continue
            qty = 0.0
            for move in request.move_ids.filtered(
                    lambda r: r.state != 'cancel'):
                qty += move.product_qty

            if float_compare(qty, request.product_qty,
                             precision_digits=precision) >= 0:
                continue

            values = request._prepare_procurement_values(
                group_id=request.procurement_group_id)
            try:
                # We launch with sudo because potentially we could create
                # objects that the user is not authorized to create, such
                # as PO.
                self.env['procurement.group'].sudo().run(
                    request.product_id, request.product_uom_qty,
                    request.product_uom_id,
                    request.location_id, request.name,
                    request.name, values)
            except UserError as error:
                errors.append(error.name)
        if errors:
            raise UserError('\n'.join(errors))
        return True

    @api.multi
    def action_view_transfer(self):
        action = self.env.ref('stock.action_picking_tree_all').read()[0]

        pickings = self.mapped('picking_ids')
        if len(pickings) > 1:
            action['domain'] = [('id', 'in', pickings.ids)]
        elif pickings:
            action['views'] = [
                (self.env.ref('stock.view_picking_form').id, 'form')]
            action['res_id'] = pickings.id
        return action

    @api.model
    def create(self, vals):
        upd_vals = vals.copy()
        if upd_vals.get('name', '/') == '/':
            upd_vals['name'] = self.env['ir.sequence'].next_by_code(
                'stock.request')
        return super().create(upd_vals)

    @api.multi
    def unlink(self):
        if self.filtered(lambda r: r.state != 'draft'):
            raise UserError(_('Only requests on draft state can be unlinked'))
        return super(StockRequest, self).unlink()
