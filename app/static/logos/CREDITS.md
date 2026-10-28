# Product marks

These are third-party trademarks, used only to identify which integration a
tile, a column or a status dot is about. Their presence is not an endorsement,
an affiliation or a claim of ownership, and nothing here is redrawn or
recoloured. Settings says the same in one line where a reader can see it,
which is where a statement like that belongs — a file in a repository is not
somewhere a customer looks.

`app/interfaces/web.py` holds `VENDOR_MARKS`, the one list of how each vendor
is drawn. A vendor here has a file; a vendor that is not here is drawn as a
coloured tile with its initial on it.

| File | Mark | Source |
| --- | --- | --- |
| `ninjaone-*` | NinjaOne | NinjaOne media kit. |
| `bitdefender-*` | Bitdefender | Bitdefender website. |
| `connectsecure-*` | ConnectSecure | ConnectSecure website. |

All three reached this repository through the Desk Aurora design mockup, and
the sources above are the ones its own credits named. **Nobody here has
checked them against the vendors' current brand pages**, which is the step to
take before this is in front of a customer: each of these vendors publishes
brand guidelines saying what may be used and how.

## What is a monogram instead, and why

| Mark | Why |
| --- | --- |
| Autotask | The file we held came from Wikimedia under CC BY-SA 3.0, whose share-alike and attribution terms are not ones to carry into a product over an icon. Removed. |
| Microsoft 365 | Microsoft's trademark and brand guidelines are strict about which marks may be shown, in what form and with what spacing, and a 22px tile in a table is not a shape they sanction. Removed. |
| Cove | N-able publishes no mark we hold. |

A monogram is a tile with a letter on it in a colour of our choosing. It is
our drawing, it claims nothing, and it is the honest answer for a vendor whose
artwork we have no clear licence to redistribute. Moving any of the three above
to a monogram is one line in `VENDOR_MARKS`: drop its `file`, give it a
`letter` and a `tint`.

## The font

`../fonts/inter-latin.woff2` is Inter by Rasmus Andersson, under the SIL Open
Font License 1.1. The licence text is beside the file, which is what that
licence asks for.
