## Deze bevinding: lege waarden in een verplichte kolom

Zoek uit of de lege waarden willekeurig verspreid zijn of bij een herkenbare groep horen (een tijdvak, een status, een betaalwijze). Ga na of de ontbrekende waarde elders in de batch staat. Voorbeelden: het totaal van een betaling is soms af te leiden uit de orderregels van dezelfde order; een klant-id of product-id is nergens uit af te leiden. Rijen waarvan de sleutel naar een andere tabel ontbreekt zijn onbruikbaar en nemen bij verwijdering hun orderregels en betalingen mee.
