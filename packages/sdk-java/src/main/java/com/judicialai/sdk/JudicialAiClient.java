package com.judicialai.sdk;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.UUID;

/**
 * Minimal Java 17 client for the Judicial AI Platform.
 * JSON payloads are passed as strings so the SDK has no runtime dependencies.
 */
public final class JudicialAiClient {
    private final String baseUrl;
    private final String apiKey;
    private final HttpClient httpClient;
    private final Duration timeout;

    private JudicialAiClient(Builder builder) {
        this.baseUrl = builder.baseUrl.replaceAll("/+$", "");
        this.apiKey = builder.apiKey;
        this.timeout = builder.timeout;
        this.httpClient = HttpClient.newBuilder().connectTimeout(builder.timeout).build();
    }

    public static Builder builder(String baseUrl, String apiKey) {
        return new Builder(baseUrl, apiKey);
    }

    public String health() throws Exception {
        return request("GET", "/healthz", null);
    }

    public String chat(String jsonBody) throws Exception {
        return request("POST", "/v1/chat/completions", jsonBody);
    }

    public String retrieve(String jsonBody) throws Exception {
        return request("POST", "/v1/retrieve", jsonBody);
    }

    public String invoke(String capabilityCode, String jsonBody) throws Exception {
        return request("POST", "/api/v1/capabilities/" + capabilityCode + "/invoke", jsonBody);
    }

    private String request(String method, String path, String body) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + path))
                .timeout(timeout)
                .header("Accept", "application/json")
                .header("X-Request-Id", "sdk-" + UUID.randomUUID().toString().replace("-", ""));
        if (apiKey != null && !apiKey.isBlank()) {
            builder.header("Authorization", "Bearer " + apiKey);
        }
        if (body == null) {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            builder.header("Content-Type", "application/json");
            builder.method(method, HttpRequest.BodyPublishers.ofString(body));
        }
        HttpResponse<String> response = httpClient.send(
                builder.build(), HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() >= 400) {
            throw new JudicialAiException(
                    response.statusCode(),
                    response.headers().firstValue("X-Request-Id").orElse(""),
                    response.body());
        }
        return response.body();
    }

    public static final class Builder {
        private final String baseUrl;
        private final String apiKey;
        private Duration timeout = Duration.ofSeconds(30);

        private Builder(String baseUrl, String apiKey) {
            this.baseUrl = baseUrl;
            this.apiKey = apiKey;
        }

        public Builder timeout(Duration timeout) {
            this.timeout = timeout;
            return this;
        }

        public JudicialAiClient build() {
            return new JudicialAiClient(this);
        }
    }
}
