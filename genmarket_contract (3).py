# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
GenMarket — a GenLayer Intelligent Contract for a decentralized marketplace.

Maps directly onto the GenMarket frontend (index.html):
  - register_as_vendor()           -> "Calling register_as_vendor() on contract..."
  - create_listing(...)            -> "Calling create_listing() on contract..."
  - update_listing(...)            -> "update_listing() will be called"
  - ai_moderate_listing(id)        -> "Running ai_moderate_listing(id) — AI checking policy..."
  - place_order(...)               -> "Transaction sent to GenLayer contract..." (escrow)
  - confirm_delivery(order_id)     -> "confirm_delivery(orderId) — releasing escrow..."
  - raise_dispute(order_id, ...)   -> "raise_dispute(orderId) on contract..."
  - resolve_dispute(order_id)      -> AI arbitration (not wired in the demo UI, but the
                                       natural next call after a dispute is raised)
  - leave_review(...)              -> "leave_review(orderId, rating, ...) on contract..."
  - send_message(...)              -> "Message sent on-chain via send_message()"

Two decisions are delegated to AI-validator consensus via the Equivalence Principle:
  1. ai_moderate_listing — is a new listing safe to keep active?
  2. resolve_dispute      — should escrowed GEN go back to the buyer or on to the vendor?

Everything else (CRUD on listings/orders, escrow bookkeeping, access control) is plain
deterministic Python — GenLayer is only used where a shared, adjudicated judgment call
actually needs consensus, per GenLayer's own "when to use GenLayer" guidance.
"""

import re

from genlayer import *
from dataclasses import dataclass


# ── Storage types ──────────────────────────────────────────────────────────
# GenLayer persisted fields can't use plain `list`/`dict`/`int` — DynArray,
# TreeMap and sized integers are used instead. Custom structs need
# @allow_storage + @dataclass.

# Basis-point resolution for arbitrated escrow splits (resolve_dispute).
# Plain module-level constant — not contract storage, no need to persist it.
BPS_DENOMINATOR = 10_000


@allow_storage
@dataclass
class Listing:
    id: u32
    vendor: Address
    title: str
    description: str
    price_gen: u256          # wei, 1 GEN = 10**18 wei
    quantity: u32
    active: bool
    category: str


@allow_storage
@dataclass
class Order:
    id: u32
    listing_id: u32
    buyer: Address
    vendor: Address
    amount_paid: u256        # held in escrow (this contract's GEN balance) until resolved
    status: str              # "pending" | "completed" | "disputed" | "refunded"
    buyer_note: str
    dispute_reason: str


@allow_storage
@dataclass
class Message:
    sender: Address
    content: str


@allow_storage
@dataclass
class Review:
    order_id: u32
    buyer: Address
    rating: u8                # 1-5
    comment: str


# ── Contract ────────────────────────────────────────────────────────────────

class GenMarket(gl.Contract):
    admin: Address
    listings: DynArray[Listing]
    orders: DynArray[Order]
    messages: TreeMap[u32, DynArray[Message]]   # order_id -> thread
    reviews: DynArray[Review]
    vendors: TreeMap[Address, bool]

    def __init__(self):
        self.admin = gl.message.sender_address

    # ── internal helpers ────────────────────────────────────────────────────

    def _require_vendor(self, addr: Address) -> None:
        if not self.vendors.get(addr, False):
            raise gl.vm.UserError("Caller is not a registered vendor")

    def _order_at(self, order_id: u32) -> Order:
        if order_id >= u32(len(self.orders)):
            raise gl.vm.UserError("Order does not exist")
        return self.orders[order_id]

    def _listing_at(self, listing_id: u32) -> Listing:
        if listing_id >= u32(len(self.listings)):
            raise gl.vm.UserError("Listing does not exist")
        return self.listings[listing_id]

    # ── vendor management ───────────────────────────────────────────────────

    @gl.public.write
    def register_as_vendor(self) -> None:
        self.vendors[gl.message.sender_address] = True

    @gl.public.view
    def is_vendor(self, address: Address) -> bool:
        return self.vendors.get(address, False)

    # ── listings ─────────────────────────────────────────────────────────────

    @gl.public.write
    def create_listing(
        self,
        title: str,
        description: str,
        price_gen: u256,
        quantity: u32,
        category: str,
    ) -> u32:
        caller = gl.message.sender_address
        self._require_vendor(caller)
        if price_gen == u256(0):
            raise gl.vm.UserError("Price must be greater than zero")

        listing_id = u32(len(self.listings))
        self.listings.append(
            Listing(
                id=listing_id,
                vendor=caller,
                title=title,
                description=description,
                price_gen=price_gen,
                quantity=quantity,
                active=quantity > u32(0),
                category=category,
            )
        )
        return listing_id

    @gl.public.write
    def update_listing(
        self,
        listing_id: u32,
        title: str,
        description: str,
        price_gen: u256,
        quantity: u32,
        active: bool,
    ) -> None:
        listing = self._listing_at(listing_id)
        if gl.message.sender_address != listing.vendor:
            raise gl.vm.UserError("Only the listing's vendor can update it")

        stored = self.listings[listing_id]
        stored.title = title
        stored.description = description
        stored.price_gen = price_gen
        stored.quantity = quantity
        stored.active = active

    @gl.public.write
    def ai_moderate_listing(self, listing_id: u32) -> bool:
        """AI-validator consensus decides whether a listing violates policy."""
        listing = gl.storage.copy_to_memory(self._listing_at(listing_id))

        prompt = f"""
        You are a marketplace content moderator for a Web3 services marketplace
        called GenMarket. Review this listing for policy violations: illegal
        goods or services, scams, prohibited items, hate speech, or clearly
        fraudulent claims.

        Title: {listing.title}
        Category: {listing.category}
        Description: {listing.description}

        Respond as JSON: {{"safe": true/false, "reason": "<one short sentence>"}}
        """

        def leader_fn():
            return gl.nondet.exec_prompt(prompt, response_format="json")

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            my_result = leader_fn()
            return my_result["safe"] == leaders_res.calldata["safe"]

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        if not result["safe"]:
            self.listings[listing_id].active = False

        return result["safe"]

    # ── orders / escrow ──────────────────────────────────────────────────────

    @gl.public.write.payable
    def place_order(self, listing_id: u32, note: str) -> u32:
        listing = self._listing_at(listing_id)
        if not listing.active:
            raise gl.vm.UserError("Listing is not active")
        if listing.quantity == u32(0):
            raise gl.vm.UserError("Listing is sold out")

        value = gl.message.value
        if value != listing.price_gen:
            raise gl.vm.UserError("Sent value does not match listing price")

        buyer = gl.message.sender_address
        order_id = u32(len(self.orders))

        self.orders.append(
            Order(
                id=order_id,
                listing_id=listing_id,
                buyer=buyer,
                vendor=listing.vendor,
                amount_paid=value,     # GEN now sits in this contract's balance (escrow)
                status="pending",
                buyer_note=note,
                dispute_reason="",
            )
        )

        stored_listing = self.listings[listing_id]
        stored_listing.quantity = listing.quantity - u32(1)
        if stored_listing.quantity == u32(0):
            stored_listing.active = False

        return order_id

    @gl.public.write
    def confirm_delivery(self, order_id: u32) -> None:
        order = self._order_at(order_id)
        if gl.message.sender_address != order.buyer:
            raise gl.vm.UserError("Only the buyer can confirm delivery")
        if order.status != "pending":
            raise gl.vm.UserError("Order is not pending")

        gl.ContractAt(order.vendor).emit_transfer(value=order.amount_paid)
        self.orders[order_id].status = "completed"

    @gl.public.write
    def raise_dispute(self, order_id: u32, reason: str) -> None:
        order = self._order_at(order_id)
        caller = gl.message.sender_address
        if caller != order.buyer and caller != order.vendor:
            raise gl.vm.UserError("Only the buyer or vendor can raise a dispute")
        if order.status != "pending":
            raise gl.vm.UserError("Order is not pending")

        stored = self.orders[order_id]
        stored.status = "disputed"
        stored.dispute_reason = reason

    def _extract_urls(self, text: str) -> list:
        """Pure/deterministic — just parsing, safe to run outside the nondet block."""
        return re.findall(r"https?://[^\s\"'<>]+", text)

    @gl.public.write
    def resolve_dispute(self, order_id: u32) -> bool:
        """
        AI-validator consensus arbitrates a disputed order using the buyer's
        note, the stated dispute reason, the on-chain message thread, and
        any evidence pages linked in that text. Consensus binds the exact
        buyer/vendor split (in basis points), not just a refund/no-refund
        flag, so validators cannot agree on "who's right" while disagreeing
        on how much moves. Returns True if the buyer received any refund.
        """
        order = gl.storage.copy_to_memory(self._order_at(order_id))
        if order.status != "disputed":
            raise gl.vm.UserError("Order is not under dispute")

        thread = self.messages.get(order_id, DynArray[Message]())
        memory_thread = gl.storage.copy_to_memory(thread)
        transcript = "\n".join(f"{m.sender}: {m.content}" for m in memory_thread)

        # Deterministic, non-nondet step: just finding candidate evidence
        # URLs in text we already have in memory. No network I/O happens
        # here, so this is fine outside the equivalence-principle block.
        evidence_urls = self._extract_urls(
            f"{order.buyer_note}\n{order.dispute_reason}\n{transcript}"
        )[:3]  # cap fetches per resolution

        def leader_fn():
            # The web fetch is the actual nondeterministic, network-touching
            # step, so it — like the LLM call — must run *inside* the
            # leader/validator function that GenVM treats as one
            # equivalence-principle unit. Each validator re-runs this whole
            # closure independently (see validator_fn), fetching the pages
            # itself rather than trusting the leader's copy.
            evidence_chunks = []
            for url in evidence_urls:
                try:
                    page_text = gl.nondet.web.render(url, mode="text")
                except Exception:
                    page_text = "(fetch failed)"
                evidence_chunks.append(f"[{url}]\n{page_text[:2000]}")
            evidence_block = "\n\n".join(evidence_chunks) if evidence_chunks else "(none)"

            prompt = f"""
            You are arbitrating a marketplace escrow dispute on GenMarket.

            Buyer note at purchase: {order.buyer_note}
            Dispute reason: {order.dispute_reason}
            Message thread between buyer and vendor:
            {transcript if transcript else "(no messages)"}

            Linked evidence pages:
            {evidence_block}

            Decide how the escrowed funds should be split between buyer and
            vendor based on whether the evidence shows the deliverable was
            reasonably provided. A full refund, a full payout, and partial
            splits are all valid outcomes.

            Respond as JSON:
            {{"refund_bps": <integer 0-10000, GEN going back to the buyer>,
              "reasoning": "<one short sentence>"}}
            """
            result = gl.nondet.exec_prompt(prompt, response_format="json")
            # Clamp deterministically so both leader and each validator
            # normalize their own raw LLM output the same way before the
            # equality check below — this does not let a validator "round
            # towards" the leader's number, it only guards against an
            # out-of-range value from that validator's own model call.
            bps = int(result["refund_bps"])
            bps = max(0, min(bps, BPS_DENOMINATOR))
            return {"refund_bps": bps, "reasoning": result.get("reasoning", "")}

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            my_result = leader_fn()
            # Exact match required on the payout-affecting field. No
            # tolerance/near-enough comparison — a validator that computes
            # a different split does not agree with the leader, full stop.
            return my_result["refund_bps"] == leaders_res.calldata["refund_bps"]

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        refund_bps = u256(result["refund_bps"])

        stored = self.orders[order_id]
        buyer_amount = (stored.amount_paid * refund_bps) // u256(BPS_DENOMINATOR)
        vendor_amount = stored.amount_paid - buyer_amount

        if buyer_amount > u256(0):
            gl.ContractAt(stored.buyer).emit_transfer(value=buyer_amount)
        if vendor_amount > u256(0):
            gl.ContractAt(stored.vendor).emit_transfer(value=vendor_amount)

        stored.status = "refunded" if buyer_amount == stored.amount_paid else "completed"

        return buyer_amount > u256(0)

    # ── reviews ──────────────────────────────────────────────────────────────

    @gl.public.write
    def leave_review(self, order_id: u32, rating: u8, comment: str) -> None:
        order = self._order_at(order_id)
        if gl.message.sender_address != order.buyer:
            raise gl.vm.UserError("Only the buyer can leave a review")
        if order.status != "completed":
            raise gl.vm.UserError("Order must be completed before leaving a review")
        if rating < u8(1) or rating > u8(5):
            raise gl.vm.UserError("Rating must be between 1 and 5")

        self.reviews.append(
            Review(order_id=order_id, buyer=order.buyer, rating=rating, comment=comment)
        )

    # ── messages ─────────────────────────────────────────────────────────────

    @gl.public.write
    def send_message(self, order_id: u32, content: str) -> None:
        order = self._order_at(order_id)
        caller = gl.message.sender_address
        if caller != order.buyer and caller != order.vendor:
            raise gl.vm.UserError("Only the buyer or vendor can message on this order")

        if order_id not in self.messages:
            self.messages[order_id] = DynArray[Message]()
        self.messages[order_id].append(Message(sender=caller, content=content))

    # ── views (read-only, matches what the frontend renders) ───────────────────

    @gl.public.view
    def get_listings(self) -> list:
        return [self._listing_to_dict(l) for l in self.listings]

    @gl.public.view
    def get_listing(self, listing_id: u32) -> dict:
        return self._listing_to_dict(self._listing_at(listing_id))

    @gl.public.view
    def get_orders_for(self, user: Address) -> list:
        return [
            self._order_to_dict(o)
            for o in self.orders
            if o.buyer == user or o.vendor == user
        ]

    @gl.public.view
    def get_order(self, order_id: u32) -> dict:
        return self._order_to_dict(self._order_at(order_id))

    @gl.public.view
    def get_messages(self, order_id: u32) -> list:
        thread = self.messages.get(order_id, DynArray[Message]())
        return [{"sender": m.sender, "content": m.content} for m in thread]

    @gl.public.view
    def get_reviews_for_listing(self, listing_id: u32) -> list:
        result = []
        for r in self.reviews:
            order = self.orders[r.order_id]
            if order.listing_id == listing_id:
                result.append(
                    {"buyer": r.buyer, "rating": r.rating, "comment": r.comment}
                )
        return result

    # ── serialization helpers ───────────────────────────────────────────────

    def _listing_to_dict(self, l: Listing) -> dict:
        return {
            "id": l.id,
            "vendor": l.vendor,
            "title": l.title,
            "description": l.description,
            "price_gen": l.price_gen,
            "quantity": l.quantity,
            "active": l.active,
            "category": l.category,
        }

    def _order_to_dict(self, o: Order) -> dict:
        return {
            "id": o.id,
            "listing_id": o.listing_id,
            "buyer": o.buyer,
            "vendor": o.vendor,
            "amount_paid": o.amount_paid,
            "status": o.status,
            "buyer_note": o.buyer_note,
            "dispute_reason": o.dispute_reason,
        }
