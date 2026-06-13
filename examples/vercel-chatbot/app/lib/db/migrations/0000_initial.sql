CREATE TABLE IF NOT EXISTS "User" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
  "email" varchar(64) NOT NULL,
  "password" varchar(64),
  "name" text,
  "emailVerified" boolean NOT NULL DEFAULT false,
  "image" text,
  "isAnonymous" boolean NOT NULL DEFAULT false,
  "createdAt" timestamp DEFAULT now() NOT NULL,
  "updatedAt" timestamp DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS "Chat" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
  "createdAt" timestamp NOT NULL,
  "title" text NOT NULL,
  "userId" uuid NOT NULL REFERENCES "User"("id"),
  "visibility" varchar NOT NULL DEFAULT 'private'
);

CREATE TABLE IF NOT EXISTS "Message_v2" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
  "chatId" uuid NOT NULL REFERENCES "Chat"("id"),
  "role" varchar NOT NULL,
  "parts" json NOT NULL,
  "attachments" json NOT NULL,
  "createdAt" timestamp NOT NULL
);

CREATE TABLE IF NOT EXISTS "Vote_v2" (
  "chatId" uuid NOT NULL REFERENCES "Chat"("id"),
  "messageId" uuid NOT NULL REFERENCES "Message_v2"("id"),
  "isUpvoted" boolean NOT NULL,
  PRIMARY KEY ("chatId", "messageId")
);

CREATE TABLE IF NOT EXISTS "Document" (
  "id" uuid DEFAULT gen_random_uuid() NOT NULL,
  "createdAt" timestamp NOT NULL,
  "title" text NOT NULL,
  "content" text,
  "text" varchar NOT NULL DEFAULT 'text',
  "userId" uuid NOT NULL REFERENCES "User"("id"),
  PRIMARY KEY ("id", "createdAt")
);

CREATE TABLE IF NOT EXISTS "Suggestion" (
  "id" uuid DEFAULT gen_random_uuid() NOT NULL,
  "documentId" uuid NOT NULL,
  "documentCreatedAt" timestamp NOT NULL,
  "originalText" text NOT NULL,
  "suggestedText" text NOT NULL,
  "description" text,
  "isResolved" boolean NOT NULL DEFAULT false,
  "userId" uuid NOT NULL REFERENCES "User"("id"),
  "createdAt" timestamp NOT NULL,
  PRIMARY KEY ("id"),
  FOREIGN KEY ("documentId", "documentCreatedAt") REFERENCES "Document"("id", "createdAt")
);

CREATE TABLE IF NOT EXISTS "Stream" (
  "id" uuid DEFAULT gen_random_uuid() NOT NULL,
  "chatId" uuid NOT NULL,
  "createdAt" timestamp NOT NULL,
  PRIMARY KEY ("id"),
  FOREIGN KEY ("chatId") REFERENCES "Chat"("id")
);
