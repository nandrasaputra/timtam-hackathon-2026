# FE vs BE

BE: non-2xx, HTTP 500, schema mismatch, the same failure on both platforms.
FE: a crash in UI or lifecycle code, one platform, an NPE or a force unwrap in client code that is not parsing an API field.

A crash is not automatically FE. A nullable to non-null contract change makes the client crash: Kotlin NPE, missing JSON key, or a Swift force unwrap on a response model. That verdict is BE when the field used to be nullable, the client still treats it as optional, and the payload now omits it or sends null. The same crash on both platforms is stronger BE evidence. One platform only is unclear until that response model is compared with the current contract.

Verdict is FE, BE, or unclear. Name the evidence line that supports it. For a nullable to non-null crash, the next step is the squad that owns the contract, not a mobile-only patch of the unwrap.

A blank field on a screen is not FE by itself. If that field is filled from an API and the reporter did not paste the response, the verdict is unclear. Do not write FE. The action item is the command: Ask BE to send MoBox the response for this screen, and name the missing field. A required field that the contract includes and the payload omits is BE, even when only one platform shows the blank. Group Ride payment method details empty on the participant screen, with no response pasted, is this case.
