# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import frappe
from frappe import _
from frappe.utils import flt, fmt_money, formatdate

from erpnext.controllers.website_list_for_contact import get_customers_suppliers


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = True
	context.doc = frappe.get_doc(frappe.form_dict.doctype, frappe.form_dict.name)
	if context.doc.docstatus != 1:
		frappe.throw(_("Not Permitted"), frappe.PermissionError)

	context.parents = frappe.form_dict.parents
	context.doc.supplier = get_supplier()
	context.doc.rfq_links = get_link_quotation(context.doc.supplier, context.doc.name)
	unauthorized_user(context.doc.supplier)
	update_supplier_details(context)
	context.portal_doc = get_portal_doc(context.doc)
	prefill_previous_rates(context)
	context["title"] = frappe.form_dict.name


def get_portal_doc(rfq) -> dict:
	"""The RFQ fields the supplier's quotation form needs."""
	items = [{"name": item.name, "idx": item.idx, "qty": item.qty} for item in rfq.items]
	return {"name": rfq.name, "items": items}


def get_supplier():
	doctype = frappe.form_dict.doctype
	parties_doctype = "Request for Quotation Supplier" if doctype == "Request for Quotation" else doctype
	customers, suppliers = get_customers_suppliers(parties_doctype, frappe.session.user)
	if not suppliers:
		return ""

	return next(
		(supplier for supplier in suppliers if check_supplier_has_docname_access(supplier)), suppliers[0]
	)


def check_supplier_has_docname_access(supplier):
	status = True
	if frappe.form_dict.name not in frappe.get_all(
		"Request for Quotation Supplier",
		filters={"supplier": supplier},
		pluck="parent",
	):
		status = False
	return status


def unauthorized_user(supplier):
	status = check_supplier_has_docname_access(supplier) or False
	if status is False:
		frappe.throw(_("Not Permitted"), frappe.PermissionError)


def update_supplier_details(context):
	supplier_doc = frappe.get_doc("Supplier", context.doc.supplier)
	context.doc.currency = supplier_doc.default_currency or frappe.get_cached_value(
		"Company", context.doc.company, "default_currency"
	)
	context.doc.currency_symbol = frappe.db.get_value("Currency", context.doc.currency, "symbol", cache=True)
	context.doc.number_format = frappe.db.get_value(
		"Currency", context.doc.currency, "number_format", cache=True
	)
	context.doc.buying_price_list = supplier_doc.default_price_list or ""


def get_link_quotation(supplier, rfq):
	sqi = frappe.qb.DocType("Supplier Quotation Item")
	sq = frappe.qb.DocType("Supplier Quotation")
	quotation = (
		frappe.qb.from_(sqi)
		.inner_join(sq)
		.on(sqi.parent == sq.name)
		.select(sqi.parent.as_("name"), sq.status, sq.transaction_date, sq.creation)
		.distinct()
		.where((sq.docstatus < 2) & (sqi.request_for_quotation == rfq) & (sq.supplier == supplier))
		.orderby(sq.creation, order=frappe.qb.desc)
		.run(as_dict=1)
	)

	for data in quotation:
		data.transaction_date = formatdate(data.transaction_date)

	return quotation or None


def prefill_previous_rates(context):
	"""Seed each RFQ line with this supplier's previous price (floreer_app#358).

	Source order: the last submitted Supplier Quotation Item, else the last
	submitted Purchase Order Item, from the supplier `get_supplier()` resolved
	for the session user (checked by `unauthorized_user` above). Never from
	request input. Only history in the page's currency and the line's UOM
	counts, so a per-kg price never prefills a per-box line.

	The rate and amount are also written into `context.portal_doc` (serialised
	as `context.rfq_doc_json`), which rfq.js uses as `window.doc`. Without that,
	a box the supplier leaves untouched would show the price but submit a zero
	rate, because rfq.js only copies a rate into `window.doc` on a change event.
	It is built on upstream's minimal `get_portal_doc` payload, never on
	`doc.as_dict()`, so the page does not embed the other invited suppliers.
	"""
	doc = context.doc
	codes = list({d.item_code for d in doc.items if d.item_code})
	previous = {}
	if doc.supplier and codes:
		for doctype in ("Purchase Order", "Supplier Quotation"):
			# Later sources overwrite earlier ones, so the Supplier Quotation wins.
			previous.update(_latest_rates(doctype, doc.supplier, doc.currency, codes))

	portal_doc = context.portal_doc
	grand_total = 0.0
	for d, row in zip(doc.items, portal_doc["items"], strict=True):
		row["item_code"] = d.item_code
		rate = previous.get((d.item_code, d.uom))
		d.floreer_prev_rate = rate
		if rate is None:
			continue
		amount = flt(rate * flt(d.qty))
		d.floreer_prev_rate_display = f"{rate:.2f}"
		d.floreer_prev_rate_hint = fmt_money(rate, precision=2, format=doc.number_format)
		d.floreer_prev_amount_display = fmt_money(amount, precision=2, format=doc.number_format)
		row["rate"] = rate
		row["amount"] = amount
		grand_total += amount

	portal_doc["grand_total"] = grand_total
	doc.floreer_prev_grand_total_display = fmt_money(grand_total, precision=2, format=doc.number_format)
	context.rfq_doc_json = frappe.as_json(portal_doc)


def _latest_rates(doctype, supplier, currency, item_codes):
	"""{(item_code, uom): rate} from the supplier's newest submitted `doctype`."""
	parent = frappe.qb.DocType(doctype)
	child = frappe.qb.DocType(f"{doctype} Item")
	query = (
		frappe.qb.from_(child)
		.inner_join(parent)
		.on(child.parent == parent.name)
		.select(child.item_code, child.uom, child.rate)
		.where(
			(parent.docstatus == 1)
			& (parent.supplier == supplier)
			& (child.parenttype == doctype)
			& (child.item_code.isin(item_codes))
			& (child.rate > 0)
		)
		.orderby(parent.transaction_date, order=frappe.qb.desc)
		.orderby(parent.creation, order=frappe.qb.desc)
		.orderby(child.idx)
	)
	if currency:
		query = query.where(parent.currency == currency)

	rates = {}
	for row in query.run(as_dict=True):
		rates.setdefault((row.item_code, row.uom), flt(row.rate))
	return rates
