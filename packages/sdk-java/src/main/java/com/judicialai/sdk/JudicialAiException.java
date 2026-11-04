package com.judicialai.sdk;

public final class JudicialAiException extends RuntimeException {
    private final int statusCode;
    private final String requestId;

    public JudicialAiException(int statusCode, String requestId, String body) {
        super(body);
        this.statusCode = statusCode;
        this.requestId = requestId;
    }

    public int statusCode() {
        return statusCode;
    }

    public String requestId() {
        return requestId;
    }
}
