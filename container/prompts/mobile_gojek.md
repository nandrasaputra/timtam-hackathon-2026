# Mobile Gojek

Android: Kotlin, Jetpack Compose, Aloha. iOS: Swift, UIKit, Aloha iOS.
Facts about the code come from mobox-glab, not from this note.

A painful crash: the response model still decodes a field as optional, and the backend contract moved nullable to non-null. Android throws on a non-null Kotlin property. iOS force-unwraps or fails decoding. Look at the response model and the call site before calling it a UI bug.
