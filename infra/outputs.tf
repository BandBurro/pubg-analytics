output "raw_bucket" {
  description = "S3 bucket holding raw match and telemetry objects."
  value       = aws_s3_bucket.raw.id
}

output "ledger_table" {
  description = "DynamoDB table tracking collection state."
  value       = aws_dynamodb_table.ledger.name
}

output "collector_function" {
  description = "Lambda function name; invoke it manually to test."
  value       = aws_lambda_function.collector.function_name
}

output "api_key_parameter" {
  description = "Write the real PUBG API key here after the first apply."
  value       = aws_ssm_parameter.api_key.name
}

output "next_steps" {
  value = <<-EOT
    1. Write the API key (never committed, never in state):
         aws ssm put-parameter --profile ${var.aws_profile} --region ${var.region} \
           --name ${aws_ssm_parameter.api_key.name} --type SecureString \
           --overwrite --value 'YOUR_KEY'

    2. Invoke once to verify:
         aws lambda invoke --profile ${var.aws_profile} --region ${var.region} \
           --function-name ${aws_lambda_function.collector.function_name} /dev/stdout

    3. Watch it:
         aws logs tail /aws/lambda/${aws_lambda_function.collector.function_name} \
           --profile ${var.aws_profile} --region ${var.region} --follow

    To stop all charges: tofu destroy
  EOT
}
