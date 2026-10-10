import { z } from "zod";

const optionalText = z.string().trim().optional().or(z.literal(""));

export const loginSchema = z.object({
  email: z.string().trim().email("Enter a valid email address"),
  password: z.string().min(1, "Password is required"),
});

export const businessSchema = z.object({
  name: z.string().trim().min(2, "Business name is required").max(120),
  owner_name: optionalText,
  owner_phone: z
    .string()
    .trim()
    .regex(/^\+[1-9]\d{7,14}$/, "Use an international number such as +919876543210"),
  owner_email: z.string().trim().email("Enter a valid owner email").optional().or(z.literal("")),
  vertical: z.string().trim().min(1),
  plan: z.string().trim().min(1),
});

export const knowledgeSchema = z.object({
  question: optionalText,
  answer: z.string().trim().min(2, "Answer is required").max(12_000),
  category: z.string().trim().min(1),
});

export const entitySchema = z.object({
  entity_type: z.string().trim().min(1),
  name: z.string().trim().min(2, "Name is required").max(160),
  description: optionalText,
});

export const personaSchema = z.object({
  persona_name: optionalText,
  persona_tone: optionalText,
  persona_language: z.array(z.string().trim().min(1)).min(1, "Choose at least one language"),
  greeting_message: optionalText,
  custom_instructions: optionalText,
});

export const simulatorMessageSchema = z.object({
  text: z.string().trim().min(1, "Write a message").max(4_000),
  mode: z.enum(["customer", "owner"]),
  channel: z.enum([
    "whatsapp",
    "web",
    "facebook_messenger",
    "instagram_dm",
    "facebook_comment",
    "instagram_comment",
  ]),
});

export type FieldErrors = Record<string, string>;

export function validationErrors(error: z.ZodError): FieldErrors {
  return error.issues.reduce<FieldErrors>((errors, issue) => {
    const key = issue.path.join(".") || "form";
    if (!errors[key]) errors[key] = issue.message;
    return errors;
  }, {});
}
