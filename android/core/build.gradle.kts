// Platform-independent Redtooth core: protocol, transfers, directory client, language, agent.
// No dependencies, so it runs on Android and on any JVM (the tests also talk to the Python desktop).
plugins { id("org.jetbrains.kotlin.jvm") }

kotlin { jvmToolchain(17) }

dependencies {
    testImplementation("org.junit.jupiter:junit-jupiter:5.11.3")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.test {
    useJUnitPlatform()
    workingDir = rootProject.projectDir.parentFile // repo root, so interop tests find the Python desktop
}
