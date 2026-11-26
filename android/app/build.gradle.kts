plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "app.redtooth.android"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.redtooth.android"
        minSdk = 29 // Wi-Fi Aware network specifiers with PSK and port need API 29
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        // Directory used until the user sets their own in onboarding.
        // Set with -PredtoothDirectory=https://… (the GitHub build passes your REDTOOTH_DIRECTORY variable).
        val directory = (project.findProperty("redtoothDirectory") as String?)?.takeIf { it.isNotBlank() } ?: "http://10.0.2.2:8790"
        buildConfigField("String", "DEFAULT_DIRECTORY", "\"$directory\"")
    }
    buildFeatures { compose = true; buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation(project(":core"))
    implementation(platform("androidx.compose:compose-bom:2024.12.01"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.documentfile:documentfile:1.0.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
