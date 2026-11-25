package app.redtooth.core

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/** Runs the dependency-free CoreSuite under JUnit (`./gradlew :core:test`). */
class CoreSuiteTest {
    @Test fun core() = assertEquals(0, CoreSuite.runAll(), "core checks failed; see test output")
}
