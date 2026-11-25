package app.redtooth.core

/** Resolve a spoken name to a contact, ranked by name match, relation, app use and proximity (port of contacts.py). */
class PeopleResolver(val contacts: List<Contact>, val me: Person) {
    @Volatile var profiles: Map<String, Profile> = emptyMap() // normalized phone -> profile
    @Volatile var nearbyPhones: Set<String> = emptySet()

    data class Candidate(val person: Person, val score: Double, val nameScore: Double)

    fun personFor(c: Contact): Person {
        val prof = c.phones.firstNotNullOfOrNull { profiles[it] }
        return Person(c, prof, nearby = c.phones.any { it in nearbyPhones })
    }

    fun byId(id: String): Person? = contacts.firstOrNull { it.id == id }?.let(::personFor)
    fun byPhone(phone: String): Person? = Phones.normalize(phone).let { p -> contacts.firstOrNull { p in it.phones }?.let(::personFor) }

    fun allPeople(): List<Person> = contacts.map(::personFor)
        .sortedWith(compareBy<Person>({ !it.nearby }, { !it.hasApp }, { -(it.contact.lastInteraction ?: 0.0) }, { it.name }))

    fun resolve(query: String, relation: String? = null): List<Candidate> {
        val q = query.lowercase().trim()
        if (q in SELF_WORDS) return listOf(Candidate(me, 1.0, 1.0))
        val out = mutableListOf<Candidate>()
        for (c in contacts) {
            val ns = nameScore(q, c)
            if (ns <= 0) continue
            val p = personFor(c)
            var s = ns
            if (relation != null && c.relation != null && relation.equals(c.relation, ignoreCase = true)) s += 0.15
            if (p.nearby) s += 0.12
            if (p.hasApp) s += 0.08
            if (c.lastInteraction != null) s += 0.02
            out += Candidate(p, s, ns)
        }
        return out.sortedByDescending { it.score }
    }

    companion object {
        val SELF_WORDS = setOf("my", "mine", "me", "i", "myself", "our")

        fun isAmbiguous(c: List<Candidate>): Boolean {
            if (c.size < 2) return false
            val (a, b) = c[0] to c[1]
            return b.nameScore >= 0.85 * a.nameScore && (a.score - b.score) < 0.12
        }

        fun nameScore(q: String, c: Contact): Double {
            val full = c.name.lowercase()
            val parts = full.split(Regex("\\s+")).filter { it.isNotEmpty() }
            if (q == full) return 1.0
            if (c.nicknames.any { it.lowercase() == q }) return 0.9
            if (parts.isNotEmpty() && q == parts[0]) return 0.88
            if (q in parts) return 0.75
            if (full.startsWith(q) && q.length >= 3) return 0.7
            val best = (parts + full).maxOfOrNull { ratio(q, it) } ?: 0.0
            return if (best >= 0.8) 0.6 * best else 0.0
        }

        /** difflib.SequenceMatcher.ratio(): 2*matches / total, matches found by longest common blocks. */
        fun ratio(a: String, b: String): Double {
            if (a.isEmpty() && b.isEmpty()) return 1.0
            fun matches(a0: Int, a1: Int, b0: Int, b1: Int): Int {
                var best = 0; var bi = a0; var bj = b0
                for (i in a0 until a1) for (j in b0 until b1) {
                    var k = 0
                    while (i + k < a1 && j + k < b1 && a[i + k] == b[j + k]) k++
                    if (k > best) { best = k; bi = i; bj = j }
                }
                if (best == 0) return 0
                return best + matches(a0, bi, b0, bj) + matches(bi + best, a1, bj + best, b1)
            }
            return 2.0 * matches(0, a.length, 0, b.length) / (a.length + b.length)
        }
    }
}
